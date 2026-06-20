"""Django view for capturing, aligning, and cropping a score sheet."""
import base64
import json
import logging
import os

# Heavy, optional dependencies (anthropic, cv2, numpy, Pillow) are imported lazily
# inside the functions that need them. This keeps the URLconf importable — and the
# auth-gated endpoints testable — on hosts where the OCR stack isn't installed.

from django.contrib.auth.decorators import user_passes_test
from django.http import JsonResponse
from django.shortcuts import render

from ..models import Hand, Position
from ..signals import broadcast_scorer_filled, broadcast_scorer_validation
from .helpers import BASE_DIR, get_tenant, is_scorer

# ---- Configure these ------------------------------------------------------
TEMPLATE_PATH      = BASE_DIR / "static" / "template.jpg"
BBOX_SCORES        = (135, 238, 384, 1500)    # (x1, y1, x2, y2) in template coords
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
    if H is None:
        return None
    return cv2.warpPerspective(image_bgr, H, (_tpl_w, _tpl_h))


def _encode_crop(aligned, bbox):
    import cv2

    x1, y1, x2, y2 = bbox
    crop = aligned[y1:y2, x1:x2]
    ok, buf = cv2.imencode('.jpg', crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        return None
    return base64.standard_b64encode(buf.tobytes()).decode('ascii')


@user_passes_test(is_scorer)
def scan_page(request, round_nb=None, table_nb=None):
    if request.method == "POST":
        import anthropic
        import cv2

        _ensure_initialized()
        file = request.FILES.get("image")
        if not file:
            return JsonResponse({"ok": False, "error": "No image uploaded"}, status=400)
        try:
            image = _read_image(file)
        except Exception as e:
            return JsonResponse({"ok": False, "error": f"Invalid image: {e}"}, status=400)

        aligned = _align_to_template(image)
        if aligned is None:
            return JsonResponse(
                {"ok": False, "error": "Could not align with template. Try a clearer, less tilted shot."},
                status=422,
            )

        scores_b64 = _encode_crop(aligned, BBOX_SCORES)

        # DEBUG: save crop and aligned image to inspect what Claude sees
        debug_dir = BASE_DIR / "captures" / "debug_crops"
        os.makedirs(debug_dir, exist_ok=True)
        cv2.imwrite(str(debug_dir / "aligned.jpg"), aligned)
        x1, y1, x2, y2 = BBOX_SCORES
        cv2.imwrite(str(debug_dir / "crop_scores.jpg"), aligned[y1:y2, x1:x2])
        logger.info("Debug crop saved to %s", debug_dir)

        api_key = os.environ.get('ANTHROPIC_API_KEY', '')
        if not api_key:
            return JsonResponse({"ok": False, "error": "ANTHROPIC_API_KEY not configured"}, status=500)

        try:
            client = anthropic.Anthropic(api_key=api_key)
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
                output_config={
                    "format": {
                        "type": "json_schema",
                        "schema": OCR_SCHEMA,
                    },
                },
            )
            raw = message.content[0].text
            logger.info("Claude raw response: %s", raw[:2000])
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.exception("Claude returned non-JSON: %s", raw[:500])
            return JsonResponse({"ok": False, "error": f"LLM returned invalid JSON:\n{raw[:1000]}"}, status=502)
        except Exception as e:
            logger.exception("Claude API call failed")
            return JsonResponse({"ok": False, "error": str(e)}, status=502)

        logger.info("Claude parsed data: %s", data)
        return JsonResponse({"ok": True, "scores": data.get("Scores", [])})

    return render(request, "mahj/scan.html", {"round_nb": round_nb, "table_nb": table_nb})


OCR_PROMPT = (
    'Read this handwritten mahjong score sheet. '
    'For each hand (rows 1-16), extract three columns: '
    'Value (integer, 8 or more), Winner (integer 1-4), '
    'Discarder (integer 1-4, or null if the cell is empty, crossed out, '
    'or contains any non-digit symbol). '
    'If a hand was not played, set all three to null. '
    'For each hand, set Confidence to a float between 0.0 (unreadable, '
    'pure guess) and 1.0 (perfectly legible, fully certain).'
)

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
                    "Winner": {"type": ["integer"]},
                    "Discarder": {"type": ["integer", "null"]},
                    "Confidence": {"type": "number"},
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


WINDS = ['E', 'S', 'W', 'N']


@user_passes_test(is_scorer)
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


@user_passes_test(is_scorer)
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

    force = body.get('force', False)
    existing = Hand.objects.filter(
        tenant=tenant, round_nb=round_nb, table_nb=table_nb,
    ).exclude(hand_nb=17)
    already_filled = existing.filter(pts__gt=0).exists()
    if already_filled and not force:
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
        hand, _ = Hand.objects.get_or_create(
            tenant=tenant, round_nb=round_nb, table_nb=table_nb, hand_nb=hand_nb,
            defaults={'pts': 0, 'win_by': 0, 'win_from': 0},
        )
        hand.pts = int(value) if value is not None else 0
        hand.win_by = int(winner) if winner is not None else 0
        hand.win_from = int(discarder) if discarder is not None else 0
        hand.confidence = float(confidence) if confidence is not None else 1.0
        hand.save()

    validate = body.get('validate', True)
    valid_hand, _ = Hand.objects.get_or_create(
        tenant=tenant, round_nb=round_nb, table_nb=table_nb, hand_nb=17,
        defaults={'pts': 0, 'win_by': 0, 'win_from': 0},
    )
    valid_hand.pts = 1 if validate else 0
    valid_hand.save()

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
