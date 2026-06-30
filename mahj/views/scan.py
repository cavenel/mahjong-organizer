"""Django view for capturing, aligning, and cropping a score sheet."""
import base64
import json
import logging
import os

# Heavy, optional dependencies (anthropic, cv2, numpy, Pillow) are imported lazily
# inside the functions that need them. This keeps the URLconf importable — and the
# endpoints testable — on hosts where the OCR stack isn't installed.

from django.conf import settings
from django.db.models import F
from django.http import JsonResponse
from django.shortcuts import render

from ..models import Hand, Position
from ..signals import broadcast_scorer_filled, broadcast_scorer_validation
from .helpers import BASE_DIR, get_tenant, is_scorer

# ---- Configure these ------------------------------------------------------
TEMPLATE_PATH      = BASE_DIR / "static" / "template.jpg"
BBOX_SCORES        = (61, 161, 241, 991) # (x1, y1, x2, y2) in template coords
MAX_FEATURES       = 5000
GOOD_MATCH_PERCENT = 0.15
MIN_MATCHES        = 12
# ---------------------------------------------------------------------------

# Lazy-initialized on first request so a missing template.png doesn't crash startup.
_orb = None
_tpl_kp = None
_tpl_des = None
_tpl_h = None
_tpl_w = None
_matcher = None


def _ensure_initialized():
    import cv2

    global _orb, _tpl_kp, _tpl_des, _tpl_h, _tpl_w, _matcher
    if _orb is not None:
        return
    template = cv2.imread(str(TEMPLATE_PATH))
    if template is None:
        raise RuntimeError(f"Template not found at {TEMPLATE_PATH}")
    tpl_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
    _tpl_h, _tpl_w = tpl_gray.shape
    _orb = cv2.ORB_create(MAX_FEATURES)
    _tpl_kp, _tpl_des = _orb.detectAndCompute(tpl_gray, None)
    _matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)


def _read_image(file_storage):
    import cv2
    import numpy as np
    from PIL import Image, ImageOps

    img = Image.open(file_storage)
    img = ImageOps.exif_transpose(img).convert("RGB")
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def _align_to_template(image_bgr):
    """Return the homography mapping photo pixels to template coordinates, or None."""
    import cv2
    import numpy as np

    h, w = image_bgr.shape[:2]
    scale = 1600 / max(h, w) if max(h, w) > 1600 else 1.0
    work = cv2.resize(image_bgr, (int(w * scale), int(h * scale))) if scale != 1.0 else image_bgr
    gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)

    kp, des = _orb.detectAndCompute(gray, None)
    if des is None or len(des) < MIN_MATCHES:
        return None

    matches = sorted(_matcher.match(des, _tpl_des), key=lambda m: m.distance)
    good = matches[: max(int(len(matches) * GOOD_MATCH_PERCENT), MIN_MATCHES)]
    if len(good) < MIN_MATCHES:
        return None

    # Scale src points back to original-image coordinates so we warp at full res
    src = np.float32([kp[m.queryIdx].pt for m in good]).reshape(-1, 1, 2) / scale
    dst = np.float32([_tpl_kp[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

    H, _ = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
    return H


def _warp_crop(image_bgr, H, bbox, max_edge=1568):
    """Warp the photo straight to the bbox region, scaled so its long edge is at most
    max_edge. Folding the scale into the homography keeps it a single resampling pass
    and bounds the output regardless of template or bbox size (Sonnet caps vision at
    ~1568px / ~1.15MP and would otherwise downscale server-side). Never upscales."""
    import cv2
    import numpy as np

    x1, y1, x2, y2 = bbox
    bw, bh = x2 - x1, y2 - y1
    s = min(1.0, max_edge / max(bw, bh))
    # Shift the bbox origin to (0,0), then scale; compose onto H so it's one warp.
    S = np.array([[s, 0.0, -x1 * s],
                  [0.0, s, -y1 * s],
                  [0.0, 0.0, 1.0]])
    return cv2.warpPerspective(image_bgr, S @ H, (round(bw * s), round(bh * s)))


def _encode_jpeg(crop):
    import cv2

    ok, buf = cv2.imencode('.jpg', crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        return None
    return base64.standard_b64encode(buf.tobytes()).decode('ascii')


def scan_page(request, round_nb=None, table_nb=None):
    if request.method == "POST":
        # Stage the image and hand OCR off to a scan_worker via the queue, then
        # return immediately. The heavy OpenCV + LLM work never runs on a request
        # worker, so parallel scans can't starve score entry or the displays.
        # The client polls scan_status for the result.
        from .. import scan_queue
        file = request.FILES.get("image")
        if not file:
            return JsonResponse({"ok": False, "error": "No image uploaded"}, status=400)
        tenant = get_tenant(request)
        try:
            job_id = scan_queue.stage_image(file.read())
            scan_queue.enqueue({
                'job_id': job_id,
                'round_nb': round_nb,
                'table_nb': table_nb,
                'subdomain': tenant.subdomain if tenant else '',
            })
        except Exception:
            logger.exception("Failed to enqueue scan job")
            return JsonResponse(
                {"ok": False, "error": "Scan service is unavailable. Try again."}, status=503)
        return JsonResponse({"ok": True, "job_id": job_id})

    # Scorers get an inline "Open score sheet" overlay; everyone else (the page is
    # public) is pointed to the admin console instead, since the sheet is gated.
    return render(request, "mahj/scan.html", {
        "round_nb": round_nb,
        "table_nb": table_nb,
        "can_open_sheet": is_scorer(request.user),
    })


def scan_status(request):
    """Poll a queued OCR job: pending / done(scores) / error / expired."""
    from .. import scan_queue
    job_id = request.GET.get('job_id', '')
    if not job_id:
        return JsonResponse({"ok": False, "error": "job_id required"}, status=400)

    result = scan_queue.get_result(job_id)
    if result is None:
        # Result TTL elapsed, or a worker crashed mid-job and never wrote one.
        return JsonResponse({"ok": False, "status": "expired",
                             "error": "Scan timed out or was lost. Re-take the photo."})
    status = result.get('status')
    if status == 'pending':
        return JsonResponse({"ok": True, "status": "pending"})
    if status == 'done':
        return JsonResponse({"ok": True, "status": "done", "scores": result.get('scores', [])})
    return JsonResponse({"ok": False, "status": "error",
                         "error": result.get('error', 'Could not read the sheet.')})


OCR_PROMPT = (
    'Read this handwritten mahjong score sheet. '
    'For each hand (rows 1-16), extract three columns: '
    'Value (integer, 8 or more), Winner (integer 1-4), '
    'Discarder (integer 1-4, or null if the cell is empty, crossed out, '
    'or contains any non-digit symbol). '
    'If a hand was not played, set all three to null. '
    '\n\n'
    'Confidence flags rows a reviewer should double-check. Be SPARING: most '
    'hands are perfectly readable and must be "certain" (they get no '
    'highlight). Only drop below "certain" when you can name a SPECIFIC other '
    'digit the writing could realistically be — an actual misread risk, not '
    'just slightly messy ink. The classic confusions are 0 vs 6, 1 vs 7, '
    '1 vs 4, 3 vs 8, 5 vs 6, 4 vs 9, 6 vs 8. Judge all three cells of the hand '
    'and report the LEAST confident as the hand Confidence, so one doubtful '
    'digit flags the row. '
    'Levels: '
    '"certain" = you can read every digit; one clear interpretation. '
    '"likely" = your reading is probably right, but a specific other digit is '
    'possible. '
    '"unsure" = two digits are genuinely plausible and you cannot decide. '
    '"guess" = barely legible. '
    'If most rows come out as "likely" you are being too cautious — reserve it '
    'for genuine confusions so the flagged rows actually stand out.'
)

# Verbal levels calibrate far better than a raw 0-1 float (models cluster floats
# near 1.0). Mapped to a tint alpha of 1 - value in the review sheet, so "likely"
# is already a visible pink and "guess" is near-solid red.
CONFIDENCE_LEVELS = {'certain': 1.0, 'likely': 0.6, 'unsure': 0.3, 'guess': 0.0}

OCR_SCHEMA = {
    "type": "object",
    "properties": {
        "Scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "Hand": {"type": "integer"},
                    "Value": {"type": ["integer", "null"]},
                    "Winner": {"type": ["integer", "null"]},
                    "Discarder": {"type": ["integer", "null"]},
                    "Confidence": {"type": "string",
                                   "enum": ["certain", "likely", "unsure", "guess"]},
                },
                "required": ["Hand", "Value", "Winner", "Discarder", "Confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["Scores"],
    "additionalProperties": False,
}

logger = logging.getLogger(__name__)


def _first_text(message):
    """Return the first text block's text — don't assume block 0 is text."""
    for block in message.content:
        if getattr(block, 'type', None) == 'text':
            return block.text
    raise ValueError("OCR response contained no text block")


def run_scan(image_bgr):
    """Align a score-sheet photo to the template and OCR the score columns.

    Worker-side logic with no request/response coupling: returns a result dict
    ready to store for polling — {'status': 'done', 'scores': [...]} or
    {'status': 'error', 'error': msg}. Runs in the scan_worker process.
    """
    _ensure_initialized()
    H = _align_to_template(image_bgr)
    if H is None:
        return {'status': 'error',
                'error': 'Could not align with template. Try a clearer, less tilted shot.'}

    scores_crop = _warp_crop(image_bgr, H, BBOX_SCORES)
    scores_b64 = _encode_jpeg(scores_crop)

    # Only dump crops for offline inspection in DEBUG — never in production.
    if settings.DEBUG:
        import cv2
        debug_dir = BASE_DIR / "captures" / "debug_crops"
        os.makedirs(debug_dir, exist_ok=True)
        cv2.imwrite(str(debug_dir / "aligned.jpg"), cv2.warpPerspective(image_bgr, H, (_tpl_w, _tpl_h)))
        cv2.imwrite(str(debug_dir / "crop_scores.jpg"), scores_crop)

    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return {'status': 'error', 'error': 'ANTHROPIC_API_KEY not configured'}

    try:
        import anthropic
        # Bounded timeout so one slow API call can't pin the single-job worker.
        client = anthropic.Anthropic(api_key=api_key, timeout=30.0, max_retries=1)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Image: the score columns (Value, Winner, Discarder for hands 1-16)."},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": scores_b64}},
                    {"type": "text", "text": OCR_PROMPT},
                ],
            }],
            output_config={"format": {"type": "json_schema", "schema": OCR_SCHEMA}},
        )
        raw = _first_text(message)
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.exception("Claude returned non-JSON")
        return {'status': 'error', 'error': 'OCR returned invalid data. Re-take the photo.'}
    except Exception as e:
        logger.exception("Claude API call failed")
        return {'status': 'error', 'error': str(e)}

    return {'status': 'done', 'scores': data.get("Scores", [])}


WINDS = ['E', 'S', 'W', 'N']


def scan_positions(request):
    """Return positions (players, MP, TP) for a given round/table."""
    tenant = get_tenant(request)
    round_nb = request.GET.get('round_nb')
    table_nb = request.GET.get('table_nb')
    if not round_nb or not table_nb:
        return JsonResponse({"ok": False, "error": "round_nb and table_nb required"}, status=400)

    positions = Position.objects.filter(
        tenant=tenant, round_nb=int(round_nb), table_nb=int(table_nb),
    ).order_by('position').select_related('player')

    has_hands = Hand.objects.filter(
        tenant=tenant, round_nb=int(round_nb), table_nb=int(table_nb),
    ).exclude(hand_nb=17).filter(pts__gt=0).exists()

    valid_hand = Hand.objects.filter(
        tenant=tenant, round_nb=int(round_nb), table_nb=int(table_nb),
        hand_nb=17, pts=1,
    ).exists()

    data = []
    for p in positions:
        data.append({
            'position': p.position,
            'wind': WINDS[p.position - 1] if 1 <= p.position <= 4 else '',
            'player': f"{p.player.rand_id}. {p.player.first_name}",
            'mp': p.minipoints,
            'tp': float(p.tablepoints) if p.tablepoints is not None else None,
        })

    return JsonResponse({
        "ok": True,
        "positions": data,
        "has_hands": has_hands,
        "validated": valid_hand,
    })


def _write_hand(tenant, round_nb, table_nb, hand_nb, fields):
    """Write a Hand cell under the optimistic-lock convention: bump version
    atomically on update so a concurrent per-cell update_hand_points sees a
    version change and gets a 409 instead of being silently clobbered. Falls
    back to create() for a first-touch row (the unique_hand_per_cell constraint
    makes the loser of a create race raise rather than double-insert)."""
    updated = Hand.objects.filter(
        tenant=tenant, round_nb=round_nb, table_nb=table_nb, hand_nb=hand_nb,
    ).update(version=F('version') + 1, **fields)
    if not updated:
        defaults = {'pts': 0, 'win_by': 0, 'win_from': 0}
        defaults.update(fields)
        Hand.objects.create(
            tenant=tenant, round_nb=round_nb, table_nb=table_nb, hand_nb=hand_nb,
            **defaults,
        )


def scan_prefill(request):
    """Write OCR-extracted hand data to DB, mark as valid."""
    if request.method != 'POST':
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    try:
        body = json.loads(request.body)
    except (ValueError, KeyError):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    tenant = get_tenant(request)
    round_nb = int(body['round_nb'])
    table_nb = int(body['table_nb'])
    scores = body.get('scores', [])

    # A filled table is never overwritten by a scan: anyone (including
    # unregistered users) may scan an empty table, but existing data can only be
    # changed on the score sheet. Clear it there to re-scan.
    existing = Hand.objects.filter(
        tenant=tenant, round_nb=round_nb, table_nb=table_nb,
    ).exclude(hand_nb=17)
    already_filled = existing.filter(pts__gt=0).exists()
    if already_filled:
        return JsonResponse({
            "ok": False,
            "conflict": True,
            "error": f"Round {round_nb} Table {table_nb} already has data.",
        }, status=409)

    for entry in scores:
        hand_nb = int(entry['Hand'])
        if hand_nb < 1 or hand_nb > 16:
            continue
        value = entry.get('Value')
        winner = entry.get('Winner')
        discarder = entry.get('Discarder')
        confidence = entry.get('Confidence')
        fields = {
            'pts': int(value) if value is not None else 0,
            'win_by': int(winner) if winner is not None else 0,
            'win_from': int(discarder) if discarder is not None else 0,
            'confidence': CONFIDENCE_LEVELS.get(confidence, 0.3),
        }
        _write_hand(tenant, round_nb, table_nb, hand_nb, fields)

    validate = body.get('validate', True)
    _write_hand(tenant, round_nb, table_nb, 17, {'pts': 1 if validate else 0})

    subdomain = tenant.subdomain if tenant else ''
    if validate:
        broadcast_scorer_validation(subdomain, {
            'type': 'scorer.validation',
            'round_nb': round_nb,
            'table_nb': table_nb,
            'valid': True,
        })
    else:
        filled = Hand.objects.filter(
            tenant=tenant, round_nb=round_nb, table_nb=table_nb, pts__gt=0,
        ).exclude(hand_nb=17).exists()
        broadcast_scorer_filled(subdomain, {
            'type': 'scorer.filled',
            'round_nb': round_nb,
            'table_nb': table_nb,
            'filled': filled,
        })

    return JsonResponse({"ok": True})
