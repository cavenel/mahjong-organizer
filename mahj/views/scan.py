"""Django view for capturing, aligning, and cropping a score sheet."""
import base64
import hashlib
import json
import logging
import os
from collections import OrderedDict

# Heavy, optional dependencies (anthropic, cv2, numpy, Pillow) are imported lazily
# inside the functions that need them. This keeps the URLconf importable — and the
# endpoints testable — on hosts where the OCR stack isn't installed.

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import F
from django.http import Http404, JsonResponse
from django.shortcuts import render

from .. import scan_key
from ..models import Hand, ScoreSheet, Seat
from ..signals import broadcast_scorer_filled, broadcast_scorer_validation
from .helpers import BASE_DIR, get_tenant, has_role, is_tenant_admin, json_body
from .score_entry import _parse_hand, _prune_to_played_hands
from ..scoring import WIND_LETTERS, _attach_players


# Shown to anyone who opens /scan for a tournament with no API key of its own.
# Says what to do instead, and who can change it, without naming a provider or a
# credential to an anonymous reader.
SCAN_OFF_MESSAGE = ("Photo scanning is not switched on for this tournament. Enter the "
                    "scores by hand, or ask the organisers to switch it on.")


def _require_scan_enabled():
    """Scan/OCR is disabled in the standalone build (no Redis queue / OCR deps).
    Guard every scan endpoint so a stray request can't reach the missing stack."""
    if not getattr(settings, 'SCAN_ENABLED', True):
        raise Http404

# ---- Sheet matching --------------------------------------------------------
# There is no built-in sheet: a tournament scans against the sheet it uploaded,
# or it does not scan. `static/template.jpg` is still shipped, but only as an
# example an organiser can print if they have no sheet of their own — it is
# offered as a download, never used as a silent fallback. A wrong template fails
# every photo with a message that blames the photographer, so "which sheet is
# this?" must always have an answer somebody chose on purpose.
EXAMPLE_SHEET_PATH = BASE_DIR / "static" / "template.jpg"
# The example sheet's own crop box, offered as the canvas's starting rectangle.
EXAMPLE_SHEET_BBOX = (61, 161, 241, 991)
MAX_FEATURES       = 5000
GOOD_MATCH_PERCENT = 0.15
MIN_MATCHES        = 12
# ---------------------------------------------------------------------------


class Template:
    """A sheet to align photos against: its ORB keypoints, size and crop box.

    Building one costs an ORB pass over a full-page image, so they are cached by
    the *content* hash of the image (see _template_for) rather than rebuilt per
    job — and rather than kept in module globals, which is what confined the
    whole install to one sheet.
    """
    __slots__ = ('etag', 'kp', 'des', 'h', 'w', 'bbox')

    def __init__(self, etag, kp, des, h, w, bbox):
        self.etag, self.kp, self.des = etag, kp, des
        self.h, self.w, self.bbox = h, w, bbox


# etag -> Template, most-recently-used last. Bounded because the four scan_worker
# replicas are long-lived processes: one entry per tenant that ever scanned would
# be a slow leak against the ~0.5 GB each is budgeted in docker-compose. Four is
# generous for a host serving one venue at a time; the image dominates the entry,
# not the ~160 KB of descriptors.
_TPL_CACHE = OrderedDict()
_TPL_CACHE_MAX = 4

# Shared by every alignment; stateless with crossCheck=False.
_matcher = None


def _orb():
    import cv2
    return cv2.ORB_create(MAX_FEATURES)


def _build_template(etag, image_bgr, bbox):
    import cv2

    global _matcher
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    kp, des = _orb().detectAndCompute(gray, None)
    if des is None or len(kp) < MIN_MATCHES:
        raise ValueError("that sheet image has too little detail to match against")
    if _matcher is None:
        _matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    return Template(etag, kp, des, h, w, bbox)


def _cache_template(tpl):
    _TPL_CACHE[tpl.etag] = tpl
    _TPL_CACHE.move_to_end(tpl.etag)
    while len(_TPL_CACHE) > _TPL_CACHE_MAX:
        _TPL_CACHE.popitem(last=False)
    return tpl


# Decoding is split out from ORB construction so each can be exercised on its own —
# and so the cache's own logic is testable on a host without OpenCV.
def _decode_template(raw):
    import cv2
    import numpy as np
    return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)


def _template_for(etag, image_bytes, bbox):
    """The Template for one tenant's uploaded sheet, built on first use.

    Keyed by content hash, not by subdomain: two tenants handing in the same
    federation sheet share one entry, and a tenant that re-uploads a corrected
    sheet gets a new one. A subdomain key would need explicit invalidation on
    every save; a content key cannot go stale.
    """
    tpl = _TPL_CACHE.get(etag)
    if tpl is not None and tpl.bbox == bbox:
        _TPL_CACHE.move_to_end(etag)
        return tpl
    image = _decode_template(image_bytes)
    if image is None:
        raise ValueError("the stored sheet image could not be decoded")
    return _cache_template(_build_template(etag, image, bbox))


def resolve_template(setup):
    """The Template this tenant's photos align against, or None if it has none.

    No fallback. A sheet that isn't the one the tables actually hand in fails
    every photo, silently and permanently, with an error that reads like bad
    photography — so a tournament with no sheet of its own does not scan at all,
    exactly as a tournament with no API key does not scan at all. Both are things
    an organiser must choose; neither is something the app can guess.
    """
    if setup is None or not setup.template:
        return None
    return _template_for(setup.etag, setup.template, setup.bbox)


def _read_image(file_storage):
    import cv2
    import numpy as np
    from PIL import Image, ImageOps

    img = Image.open(file_storage)
    img = ImageOps.exif_transpose(img).convert("RGB")
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def _align_to_template(image_bgr, tpl):
    """Return the homography mapping photo pixels to `tpl` coordinates, or None."""
    import cv2
    import numpy as np

    h, w = image_bgr.shape[:2]
    scale = 1600 / max(h, w) if max(h, w) > 1600 else 1.0
    work = cv2.resize(image_bgr, (int(w * scale), int(h * scale))) if scale != 1.0 else image_bgr
    gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)

    kp, des = _orb().detectAndCompute(gray, None)
    if des is None or len(des) < MIN_MATCHES:
        return None

    matches = sorted(_matcher.match(des, tpl.des), key=lambda m: m.distance)
    good = matches[: max(int(len(matches) * GOOD_MATCH_PERCENT), MIN_MATCHES)]
    if len(good) < MIN_MATCHES:
        return None

    # Scale src points back to original-image coordinates so we warp at full res
    src = np.float32([kp[m.queryIdx].pt for m in good]).reshape(-1, 1, 2) / scale
    dst = np.float32([tpl.kp[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

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
    # The LAST element, not the first. nginx used `$proxy_add_x_forwarded_for`, which
    # *appends* the real peer to whatever the client sent — so the header read
    # `<attacker value>, <real ip>` and taking [0] handed the caller control of their
    # own bucket key. Rotating it gave a fresh allowance every request, on an endpoint
    # that is anonymous by design and spends money per upload.
    #
    # This is correct because our nginx is the outermost proxy and now sets the header
    # to $remote_addr, so there is exactly one element and it is the real client. That
    # assumption is declared in nginx/mahjong.conf.template, next to the header — put
    # a CDN in front without restoring the client IP there (real_ip module) and every
    # scan lands in one bucket, throttling the venue after six uploads.
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    addr = (forwarded.split(',')[-1].strip()
            or request.META.get('REMOTE_ADDR') or 'unknown')
    key = f'scan_uploads:{hashlib.sha256(addr.encode()).hexdigest()[:32]}'
    try:
        added = cache.add(key, 1, UPLOAD_WINDOW_S)
        if added:
            return True
        if added is None:
            # IGNORE_EXCEPTIONS turns an unreachable cache into None rather than an
            # exception, so add() no longer distinguishes "someone else has the key"
            # from "there is no cache". Without this the None fell through to incr(),
            # whose None result then raised TypeError on the comparison and landed in
            # the except below — the right outcome by accident. Say it directly.
            return True
        count = cache.incr(key)
        return count is None or count <= UPLOAD_MAX_PER_WINDOW
    except Exception:
        # A cache without INCR support, or none at all: fail open rather than
        # blocking the venue's scanning.
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


def _require_seated_table(tenant, round_nb, table_nb):
    """404 unless this (round, table) is in the seating chart.

    The same guard ``admin_scores_per_hand`` carries, for the same reason: without
    it a hand-typed /scan_99_99 plus a blank sheet writes 16 Hands and a ScoreSheet
    for a table nobody ever played, which then shows up as an open sheet in the
    publisher's round overview with nothing in the UI to explain it.
    """
    if not Seat.objects.filter(tenant=tenant, round_nb=round_nb,
                               table_nb=table_nb).exists():
        raise Http404('no such table in the seating chart')


def scan_page(request, round_nb=None, table_nb=None):
    _require_scan_enabled()
    # No tenant, nothing to scan for. The apex carries no subdomain (get_domain
    # returns "") and neither does a bare-IP or localhost host, all three of which
    # ALLOWED_HOSTS admits — and an untargeted POST /scan skips the seating check, so
    # it used to stage an image and buy a vision call whose result could never be
    # written anywhere: scan_prefill rejects it for having no round/table. Anonymous
    # endpoint, paid per call, no tournament behind it. The standalone build is
    # unaffected: LOCAL_TENANT pins its tenant.
    tenant = get_tenant(request)
    if tenant is None:
        raise Http404('no tournament on this host')

    # The money gate, and deliberately the FIRST thing after the tenant check: a
    # tournament that cannot scan should not have an 8 MB body read into memory,
    # an image staged on the shared volume, or a job queued. Everything below —
    # the size limit, the rate limiter, the image sniff, the seating check — is
    # about which *accepted* photos are worth paying for; this is about whether
    # there is anyone to pay at all. One cached lookup.
    #
    # 503, not 404: the endpoint exists and the state is fixable. Not 403: there
    # is nothing wrong with the caller.
    if not scan_key.is_configured(tenant.subdomain):
        if request.method == "POST":
            return JsonResponse({"ok": False, "error": SCAN_OFF_MESSAGE}, status=503)
        # A GET still renders. Someone following a QR from a printed score sheet
        # deserves a sentence they can act on, not a blank 404.
        return render(request, "mahj/scan.html", {
            "round_nb": round_nb,
            "table_nb": table_nb,
            "can_open_sheet": has_role(request, 'scorer'),
            "scanning_off": True,
            "scanning_off_admin": is_tenant_admin(request),
        })

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
        # Last check before the image is staged and a vision call is queued: a photo
        # of a table that isn't in the chart has nowhere to land. `/scan` with no
        # coordinates is a real route (the operator picks the table afterwards), so
        # there is nothing to check for one of those.
        if round_nb and table_nb:
            _require_seated_table(tenant, round_nb, table_nb)
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
    expired = JsonResponse({"ok": False, "status": "expired",
                            "error": "Scan timed out or was lost. Re-take the photo."})
    if result is None:
        # Result TTL elapsed, or a worker crashed mid-job and never wrote one.
        return expired
    tenant = get_tenant(request)
    if (result.get('subdomain') or '') != (tenant.subdomain if tenant else ''):
        # Job ids are a flat namespace shared by every tenant, and this endpoint is
        # open — so without this a job id from another tournament returned that
        # tournament's scores. `scan_prefill` performs the same comparison before it
        # writes; polling had no equivalent.
        #
        # Answered exactly like a job that does not exist, rather than with a
        # distinct refusal: from this subdomain a foreign job *is* nothing, and one
        # response for both cases leaves nothing to probe for. The scan page already
        # handles this shape.
        return expired
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
    """Return the first text block's text — don't assume block 0 is text.

    A response with no text block at all is a real thing, not a paranoid guard:
    a model that spends its whole output budget before answering returns thinking
    blocks and nothing else.
    """
    for block in message.content:
        if getattr(block, 'type', None) == 'text':
            return block.text
    raise ValueError("OCR response contained no text block")


# One name for the model, used by both the OCR call and the admin's "Test key"
# button — they must never drift, or a key tests green and fails at the venue.
OCR_MODEL = "claude-sonnet-5"

# Reading printed digits out of a crop is transcription, not reasoning, and this
# model thinks by default — which is a real defect here, not a preference:
#   - thinking tokens share max_tokens with the answer, so the JSON came back
#     truncated mid-string, or the whole budget went to thinking and no text block
#     was emitted at all. Both surfaced as "could not read the sheet", per photo,
#     unpredictably, because adaptive thinking varies with the image.
#   - they bill at output rates, on a bill the *tenant* pays.
#   - they make every job slower, and four workers share one FIFO queue, so a
#     round-end burst backs up behind them.
# Disabled explicitly. Do not "just raise max_tokens" instead: that pays for
# reasoning nobody asked for on every scan of the event.
OCR_THINKING = {"type": "disabled"}

# Sixteen rows of five short fields is ~800 tokens. The headroom is for a model
# that decides to be chatty around the JSON, not for thinking (see above) — and
# it is checked rather than trusted: see the stop_reason guard in run_scan.
OCR_MAX_TOKENS = 4096

# What an anonymous client is allowed to be told. Nothing here may name Anthropic,
# an env var, a key, or carry raw SDK text: the scan page is public, and with
# bring-your-own-key that text can now reference a tenant's own organisation.
ERR_MISCONFIGURED = ("Scanning is not set up correctly for this tournament. The "
                     "organisers need to check the API key.")
ERR_RATE_LIMITED = "Too many scans at once. Wait a minute and take the photo again."
ERR_UPSTREAM = "The reader is busy or unreachable. Try again in a moment."
ERR_UNREADABLE = "Could not read the sheet. Re-take the photo."
ERR_NO_KEY = "Photo scanning is not available right now. Enter the scores by hand."
ERR_NO_TEMPLATE = ERR_NO_KEY

# Alignment failed. Which is *usually* a bad photo — but it can equally be the wrong
# sheet uploaded, so the old wording ("try a clearer, less tilted shot") blamed the
# photographer for what may be a configuration mistake nobody at the venue can see.
ERR_ALIGN = ("Could not match this photo to the score sheet. Photograph the whole "
             "sheet, flat and in good light. If it keeps failing, ask the organisers "
             "to check the score sheet setup.")


def align_and_crop(image_bgr, tpl):
    """Align a photo to `tpl` and return the base64 JPEG of its score columns.

    Returns (b64, None) or (None, error_dict). Shared by the OCR path and the
    admin's alignment preview, so what an organiser checks before the event is
    the same computation the venue's scans run through.
    """
    H = _align_to_template(image_bgr, tpl)
    if H is None:
        return None, {'status': 'error', 'kind': 'align', 'error': ERR_ALIGN}
    crop = _warp_crop(image_bgr, H, tpl.bbox)
    b64 = _encode_jpeg(crop)
    if b64 is None:
        return None, {'status': 'error', 'kind': 'crop', 'error': ERR_UNREADABLE}

    # Only dump crops for offline inspection in DEBUG — never in production.
    if settings.DEBUG:
        import cv2
        debug_dir = BASE_DIR / "captures" / "debug_crops"
        os.makedirs(debug_dir, exist_ok=True)
        cv2.imwrite(str(debug_dir / "aligned.jpg"),
                    cv2.warpPerspective(image_bgr, H, (tpl.w, tpl.h)))
        cv2.imwrite(str(debug_dir / "crop_scores.jpg"), crop)
    return b64, None


def _ocr_error(exc):
    """Map an SDK exception to what the client is told, and what we do about it.

    Returns (result_dict, forget_the_key). The client never sees the exception:
    `str(e)` used to go straight back through scan_status to an anonymous poller.
    """
    import anthropic

    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return {'status': 'error', 'kind': 'auth', 'error': ERR_MISCONFIGURED}, True
    if isinstance(exc, anthropic.RateLimitError):
        # Named cause, because this is the likeliest support ticket the whole
        # feature generates: a brand-new Anthropic org starts on the lowest rate
        # tier, and the first round-end burst 429s. The fix is on their account.
        return {'status': 'error', 'kind': 'rate', 'error': ERR_RATE_LIMITED}, False
    if isinstance(exc, anthropic.NotFoundError):
        logger.error("OCR model %r was not found for this key", OCR_MODEL)
        return {'status': 'error', 'kind': 'model',
                'error': "Scanning is not set up correctly for this tournament."}, False
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return {'status': 'error', 'kind': 'upstream', 'error': ERR_UPSTREAM}, False
    if isinstance(exc, anthropic.APIStatusError) and getattr(exc, 'status_code', 0) >= 500:
        return {'status': 'error', 'kind': 'upstream', 'error': ERR_UPSTREAM}, False
    return {'status': 'error', 'kind': 'unknown', 'error': ERR_UNREADABLE}, False


def run_scan(image_bgr, api_key, template):
    """Align a score-sheet photo to its template and OCR the score columns.

    Worker-side logic with no request/response coupling: no DB, no tenant object,
    no environment — the caller resolves the tenant's key and sheet and passes
    them in. Returns a result dict ready to store for polling:
    {'status': 'done', 'scores': [...]} or {'status': 'error', 'error': msg}.

    `api_key` and `template` are both required, and neither has a fallback: a
    tournament scans with its own key against its own sheet, or it does not scan.
    There is no fallback to ANTHROPIC_API_KEY, and adding
    one back — or constructing anthropic.Anthropic() without api_key=, which
    silently picks up the ambient credential — would restore the exact defect
    this whole feature exists to remove: every tenant's scans on the host's bill.
    """
    if not api_key:
        return {'status': 'error', 'kind': 'nokey', 'error': ERR_NO_KEY}
    if template is None:
        # The enqueue/dequeue race, same as a cleared key: the request path
        # already refuses a tournament with no sheet.
        return {'status': 'error', 'kind': 'notemplate', 'error': ERR_NO_TEMPLATE}

    scores_b64, err = align_and_crop(image_bgr, template)
    if err is not None:
        return err

    try:
        import anthropic
        # Bounded timeout so one slow API call can't pin the single-job worker.
        client = anthropic.Anthropic(api_key=api_key, timeout=30.0, max_retries=1)
        message = client.messages.create(
            model=OCR_MODEL,
            max_tokens=OCR_MAX_TOKENS,
            thinking=OCR_THINKING,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Image: the score columns (Value, Winner, Discarder for hands 1-16)."},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": scores_b64}},
                    # No cache_control breakpoint here, deliberately: the prompt is
                    # ~460 tokens, under the ~1024-token minimum cacheable prefix, so
                    # a breakpoint is silently ignored — nothing is written to cache
                    # and there is nothing to bill at the 1.25x write rate either.
                    # It would buy zero, not a discount.
                    {"type": "text", "text": OCR_PROMPT},
                ],
            }],
            output_config={"format": {"type": "json_schema", "schema": OCR_SCHEMA}},
        )
        # Check this *before* parsing. A truncated answer is not malformed JSON in
        # any interesting sense — it is a budget problem with a completely
        # different fix, and letting it fall through to the JSONDecodeError branch
        # told the operator to re-take a photo that was never the problem.
        if getattr(message, 'stop_reason', None) == 'max_tokens':
            logger.error("OCR hit max_tokens (%d) — the answer was truncated",
                         OCR_MAX_TOKENS)
            return {'status': 'error', 'kind': 'truncated', 'error': ERR_UNREADABLE}
        raw = _first_text(message)
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.exception("Claude returned non-JSON")
        return {'status': 'error', 'kind': 'json',
                'error': 'OCR returned invalid data. Re-take the photo.'}
    except Exception as e:
        logger.exception("Claude API call failed")
        try:
            result, _ = _ocr_error(e)
        except ImportError:
            result = {'status': 'error', 'kind': 'unknown', 'error': ERR_UNREADABLE}
        return result

    return {'status': 'done', 'scores': data.get("Scores", [])}


def run_preview(image_bgr, template):
    """Align a photo and hand back the crop, without buying an OCR call.

    This is what the Scanning admin page's "Test alignment" button runs. It goes
    through the queue like a real scan — same staging, same worker, same
    align_and_crop — precisely so that a green preview means the venue's scans
    will align too. It must never reach the API: that is asserted in the tests,
    because "the free button quietly became a billed one" is the regression this
    design invites.
    """
    if template is None:
        return {'status': 'error', 'kind': 'notemplate',
                'error': 'Upload a picture of your blank score sheet first.'}
    b64, err = align_and_crop(image_bgr, template)
    if err is not None:
        return err
    return {'status': 'done', 'preview': f'data:image/jpeg;base64,{b64}'}


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
    _require_seated_table(tenant, round_nb, table_nb)
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
    # Only ever raises the flag. A scorer's review is what validates a sheet, so a
    # later scan that didn't ask to validate must not quietly undo one — reachable
    # for a table validated with nothing played, which the filled-table gate above
    # lets through.
    sheet, created = ScoreSheet.objects.get_or_create(
        tenant=tenant, round_nb=round_nb, table_nb=table_nb,
        defaults={'validated': validate},
    )
    if validate and not created and not sheet.validated:
        sheet.validated = True
        sheet.save(update_fields=['validated'])

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
