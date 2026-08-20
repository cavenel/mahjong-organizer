"""Django view for capturing, aligning, and cropping a score sheet."""
import base64
import hashlib
import json
import logging
import os

# Heavy, optional dependencies (anthropic, cv2, numpy, Pillow) are imported lazily
# inside the functions that need them. This keeps the URLconf importable — and the
# endpoints testable — on hosts where the OCR stack isn't installed.

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import F
from django.http import Http404, JsonResponse
from django.shortcuts import render

from ..models import Hand, ScoreSheet, Seat
from ..signals import broadcast_scorer_filled, broadcast_scorer_validation
from .helpers import BASE_DIR, get_tenant, has_role, json_body
from .score_entry import _parse_hand, _prune_to_played_hands
from ..scoring import WIND_LETTERS, _attach_players


def _require_scan_enabled():
    """Scan/OCR is disabled in the standalone build (no Redis queue / OCR deps).
    Guard every scan endpoint so a stray request can't reach the missing stack."""
    if not getattr(settings, 'SCAN_ENABLED', True):
        raise Http404

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
    """Warp the photo straight to the bbox region in one resampling pass, scaled so the
    long edge is max_edge (1568 — Sonnet's vision cap; anything larger is downscaled
    server-side anyway). Folding the scale into the homography resamples the scan once,
    at the final resolution. Because the warp is a rotation/perspective, output pixels
    land between scan pixels, so a dense output grid captures the reconstructed image
    more faithfully than resampling near the scan's own pixel rate would — we always
    scale up to max_edge rather than snapping to the smaller template grid."""
    import cv2
    import numpy as np

    x1, y1, x2, y2 = bbox
    bw, bh = x2 - x1, y2 - y1
    s = max_edge / max(bw, bh)
    # Shift the bbox origin to (0,0) and scale; compose onto H so it's one warp.
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


# What an anonymous upload is allowed to cost us. Each accepted photo stages a file
# on the shared volume and buys one paid vision-API call, so the endpoint needs a
# ceiling of its own rather than relying on nginx's body limit alone.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024      # a phone photo of an A4 sheet, generously
UPLOAD_WINDOW_S = 60
UPLOAD_MAX_PER_WINDOW = 6               # per client address; a re-shoot or two is fine


def _upload_allowed(request):
    """Simple per-address rate limit on staging a scan.

    Cache-backed and best-effort: the cache is shared but not transactional, so two
    simultaneous uploads can both pass. That is fine — this exists to stop one phone
    (or one script) burning the OCR budget, not to be a precise quota. A cache
    without INCR support, or no cache at all, fails open rather than blocking the
    venue's scanning.
    """
    addr = (request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
            or request.META.get('REMOTE_ADDR') or 'unknown')
    key = f'scan_uploads:{hashlib.sha256(addr.encode()).hexdigest()[:32]}'
    try:
        if cache.add(key, 1, UPLOAD_WINDOW_S):
            return True
        return cache.incr(key) <= UPLOAD_MAX_PER_WINDOW
    except Exception:
        return True


def _looks_like_an_image(raw):
    """True if these bytes decode as an image.

    The OCR worker would fail on anything else anyway, but by then the file is staged
    and a paid vision call is queued, so junk is cheaper to reject here.

    Fails *open* when the imaging stack isn't installed: cv2/numpy are imported lazily
    all through this module precisely so these endpoints work on a host without it,
    and refusing every upload there would be worse than letting the worker decide.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return True
    try:
        return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR) is not None
    except Exception:
        return False


def scan_page(request, round_nb=None, table_nb=None):
    _require_scan_enabled()
    if request.method == "POST":
        # Stage the image and hand OCR off to a scan_worker via the queue, then
        # return immediately. The heavy OpenCV + LLM work never runs on a request
        # worker, so parallel scans can't starve score entry or the displays.
        # The client polls scan_status for the result.
        from .. import scan_queue
        file = request.FILES.get("image")
        if not file:
            return JsonResponse({"ok": False, "error": "No image uploaded"}, status=400)
        if file.size > MAX_UPLOAD_BYTES:
            mb = MAX_UPLOAD_BYTES // (1024 * 1024)
            return JsonResponse(
                {"ok": False,
                 "error": f"That photo is too large (limit {mb} MB). Take it again at a "
                          "lower resolution."}, status=413)
        if not _upload_allowed(request):
            return JsonResponse(
                {"ok": False,
                 "error": "Too many scans from this device just now. Wait a moment and "
                          "try again."}, status=429)
        raw = file.read()
        if not _looks_like_an_image(raw):
            return JsonResponse(
                {"ok": False, "error": "That file isn't a readable image. Take the photo again."},
                status=400)
        tenant = get_tenant(request)
        try:
            scan_queue.sweep_stale_images()
            job_id = scan_queue.stage_image(raw)
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
        "can_open_sheet": has_role(request, 'scorer'),
    })


def scan_status(request):
    """Poll a queued OCR job: pending / done(scores) / error / expired."""
    _require_scan_enabled()
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


def scan_seats(request):
    """Return the table's seats for a given round/table: the seat labels (draw
    number + short name) and whether the sheet is already filled/validated.

    This endpoint is anonymous (players prefill from a photo of their own
    table), so it deliberately does NOT return minipoints/tablepoints — those
    are withheld from the public until a round is published, and leaking them
    here would bypass that masking. The scan UI only needs the names and the
    filled/validated flags."""
    _require_scan_enabled()
    tenant = get_tenant(request)
    # Coerced once, up front. These are raw query parameters on a public endpoint, so
    # a non-numeric one is a routine event, not a 500 — and int() was being called on
    # them three times over besides.
    try:
        round_nb = int(request.GET['round_nb'])
        table_nb = int(request.GET['table_nb'])
    except (KeyError, TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "round_nb and table_nb required"}, status=400)

    seats = _attach_players(tenant, list(Seat.objects.filter(
        tenant=tenant, round_nb=round_nb, table_nb=table_nb,
    ).order_by('wind')))

    has_hands = Hand.objects.filter(
        tenant=tenant, round_nb=round_nb, table_nb=table_nb,
        win_by__isnull=False,
    ).exists()

    valid_hand = ScoreSheet.objects.filter(
        tenant=tenant, round_nb=round_nb, table_nb=table_nb,
        validated=True,
    ).exists()

    data = []
    for p in seats:
        first = p.player.short_name if p.player else ''
        data.append({
            'wind': WIND_LETTERS[p.wind - 1] if 1 <= p.wind <= 4 else '',
            'player': f"{p.draw_number}. {first}",
        })

    return JsonResponse({
        "ok": True,
        "seats": data,
        "has_hands": has_hands,
        "validated": valid_hand,
    })


def _write_hand(tenant, round_nb, table_nb, hand_nb, fields):
    """Write one OCR hand cell. This path does not detect conflicts itself —
    there is no ``version=`` predicate here; what keeps a concurrent scorer's
    entry from being overwritten is the caller's emptiness gate, taken under
    ``select_for_update`` in the same transaction as these writes. The
    ``F('version') + 1`` bump serves the *other* direction: a per-cell editor
    still holding the pre-scan version fails its own version check on its next
    save, instead of silently overwriting the prefill. Falls back to create()
    for a first-touch row (the unique_hand_per_cell constraint makes the loser
    of a create race raise rather than double-insert)."""
    updated = Hand.objects.filter(
        tenant=tenant, round_nb=round_nb, table_nb=table_nb, hand_nb=hand_nb,
    ).update(version=F('version') + 1, **fields)
    if not updated:
        defaults = {'points': 0, 'win_by': None, 'win_from': None}
        defaults.update(fields)
        Hand.objects.create(
            tenant=tenant, round_nb=round_nb, table_nb=table_nb, hand_nb=hand_nb,
            **defaults,
        )


def scan_prefill(request):
    """Write a finished scan job's OCR result into the score sheet.

    POST ``{job_id, validate?}``. Everything that decides *what gets written where*
    comes from the job the server staged, never from this request body:

    * The scores are read from the queue result. They used to be taken from the
      body, which meant a caller needed no photo at all — an anonymous POST could
      write hand values of its choosing into any empty table.
    * The round, table and tenant come from the job too, recorded from the scan URL
      when the image was staged. So a job cannot be redirected at a different table
      or a different tournament than the one it was photographed for.
    * ``validate`` is honoured only for a scorer, and defaults to off. It used to
      default to *on*, so an anonymous POST also marked the sheet validated —
      skipping the review this whole flow exists to feed. The scan page has always
      asked for a saved-but-unvalidated sheet, and says so on its result card.

    The job is not consumed, so a retry after a dropped response still works: a
    replay can only rewrite the table the photo was of, and the already-filled guard
    below stops the second attempt anyway.
    """
    _require_scan_enabled()
    if request.method != 'POST':
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    from .. import scan_queue
    body = json_body(request)
    job_id = str(body.get('job_id') or '').strip()
    if not job_id:
        return JsonResponse({"ok": False, "error": "job_id required"}, status=400)

    result = scan_queue.get_result(job_id)
    if result is None:
        return JsonResponse(
            {"ok": False, "status": "expired",
             "error": "Scan timed out or was lost. Re-take the photo."}, status=404)
    if result.get('status') != 'done':
        return JsonResponse(
            {"ok": False, "error": "That scan hasn't finished reading yet."}, status=409)

    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    if (result.get('subdomain') or '') != subdomain:
        # Staged on another tournament's scan page — invisible here, like any other
        # cross-tenant reference.
        raise Http404

    round_nb, table_nb = result.get('round_nb'), result.get('table_nb')
    if not round_nb or not table_nb:
        return JsonResponse(
            {"ok": False,
             "error": "That scan wasn't taken for a particular table. Use the QR code "
                      "or link on the score sheet, which carries the round and table."},
            status=400)
    scores = result.get('scores') or []

    # A filled table is never overwritten by a scan: anyone (including
    # unregistered users) may scan an empty table, but existing data can only be
    # changed on the score sheet. Clear it there to re-scan.
    #
    # One transaction for the gate and the writes, with the table's hand rows
    # locked: a scorer starting entry in the gap between "is it empty?" and the
    # writes was previously overwritten. Their sheet materializes its 16 rows on
    # open, so locking the existing rows serializes the two paths — their UPDATE
    # waits on these locks and then fails its own version predicate against the
    # bumped versions. (An unopened table has no rows to lock, but then nobody
    # is mid-entry on it either — first-touch creates race only against another
    # scan, where the unique constraint keeps it single.)
    with transaction.atomic():
        already_filled = any(
            h.win_by is not None
            for h in Hand.objects.select_for_update().filter(
                tenant=tenant, round_nb=round_nb, table_nb=table_nb)
        )
        if already_filled:
            return JsonResponse({
                "ok": False,
                "conflict": True,
                "error": f"Round {round_nb} Table {table_nb} already has data.",
            }, status=409)

        for entry in scores:
            if not isinstance(entry, dict):
                continue
            try:
                hand_nb = int(entry['Hand'])
            except (KeyError, TypeError, ValueError):
                continue        # a row the OCR couldn't place; the rest still lands
            if hand_nb < 1 or hand_nb > 16:
                continue
            confidence = entry.get('Confidence')
            fields = _parse_hand(entry.get('Value'), entry.get('Winner'), entry.get('Discarder'))
            fields['confidence'] = CONFIDENCE_LEVELS.get(confidence, 0.3)
            _write_hand(tenant, round_nb, table_nb, hand_nb, fields)

    # Validating a sheet is what takes it out of the review queue, so it needs the
    # scorer role — and is off unless explicitly asked for. Anyone may scan an empty
    # table; only a scorer may declare the result final.
    validate = bool(body.get('validate')) and has_role(request, 'scorer')
    if validate:
        _prune_to_played_hands(tenant, round_nb, table_nb)
    ScoreSheet.objects.update_or_create(
        tenant=tenant, round_nb=round_nb, table_nb=table_nb,
        defaults={'validated': bool(validate)},
    )

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
            tenant=tenant, round_nb=round_nb, table_nb=table_nb, win_by__isnull=False,
        ).exists()
        broadcast_scorer_filled(subdomain, {
            'type': 'scorer.filled',
            'round_nb': round_nb,
            'table_nb': table_nb,
            'filled': filled,
        })

    return JsonResponse({"ok": True})
