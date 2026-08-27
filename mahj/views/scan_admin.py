"""Per-tenant scanning setup: the OCR API key, and the sheet template.

The front end is the Scanning page (admin?page=scanning); resolution for the
worker lives in mahj.scan_key and the alignment itself in views.scan.

Two things are configured here and they fail in opposite ways. A missing **key**
is loud — the QR disappears and /scan says so. A wrong **template** is silent:
every photo simply fails to align, forever, and the message the player sees
blames their photography. That is why this page carries an alignment test, and
why the test runs through the real queue and the real alignment code rather than
approximating it.

The key is write-only: it is never rendered back, only the last four characters
(`key_tail`) so support can identify which key is installed.

Only the *key* endpoints require a recent password re-confirmation. The sheet
ones deliberately do not: they hold no credential, and uploading a sheet and
drawing a crop box on it takes minutes — longer than the 10-minute reauth window
— so gating them meant an organiser could lose that work to a 403 at the moment
they pressed Save. Reaching this page at all still costs a confirmation (the page
itself is reauth-gated), and the worst a stale session can do here is point a
tournament at the wrong sheet, which breaks its own scanning and nothing else.
"""
import hashlib
import logging

from django.conf import settings
from django.http import Http404, HttpResponse, JsonResponse

from .. import scan_key
from ..models import ScanConfig
from .helpers import get_tenant, tenant_admin_required
# The example sheet's crop box is offered as the canvas's starting rectangle: a
# sheet that is merely a rescaled variant then needs almost no adjustment.
from .scan import EXAMPLE_SHEET_BBOX, OCR_MODEL
from .user_admin import tenant_admin_and_reauthed

logger = logging.getLogger(__name__)

# A flat scan of an A4 sheet, generously. Bigger than this is not a score sheet.
MAX_TEMPLATE_BYTES = 4 * 1024 * 1024


def _require_scan_enabled():
    """The whole page does not exist in the standalone build."""
    if not getattr(settings, 'SCAN_ENABLED', True):
        raise Http404


def _bad(error, status=400):
    return JsonResponse({'status': 'error', 'error': error}, status=status)


@tenant_admin_and_reauthed
def scan_key_save(request):
    """Save (or clear) this tenant's OCR API key.

    Write-only, like the publish target's password: a blank field leaves the
    stored key untouched, so the form never has to echo a secret back, and
    clear_key=1 wipes it. Fields are read by name — never **request.POST — so a
    crafted form cannot reach a column this page doesn't own.
    """
    _require_scan_enabled()
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    tenant = get_tenant(request)
    if tenant is None:
        return _bad('No tournament.')
    cfg, _ = ScanConfig.objects.get_or_create(tenant=tenant)

    if request.POST.get('clear_key') == '1':
        cfg.api_key_enc = None
        cfg.key_tail = ''
        cfg.last_error = ''
        cfg.last_error_at = None
        cfg.save()
        logger.info("scan key cleared by %s (tenant %s)", request.user, tenant.subdomain)
        return JsonResponse({'status': 'ok', 'has_key': False, 'key_tail': ''})

    key = (request.POST.get('api_key') or '').strip()
    if key:
        cfg.api_key_enc = scan_key.encrypt(key)
        cfg.key_tail = key[-4:]
        # A new key deserves a clean slate: the old failure is about the old key.
        cfg.last_error = ''
        cfg.last_error_at = None
        # Never the key itself, at any level. Its length is enough to tell a
        # paste accident from a real key in a support conversation.
        logger.info("scan key set by %s (tenant %s), %d chars ending %s",
                    request.user, tenant.subdomain, len(key), key[-4:])
    cfg.save()
    return JsonResponse({'status': 'ok', 'has_key': bool(cfg.api_key_enc),
                         'key_tail': cfg.key_tail,
                         # A warning, not a refusal: key formats change, and a
                         # hard reject on a prefix is a support call waiting.
                         'warning': ('' if not key or key.startswith('sk-ant-')
                                     else 'That does not look like an Anthropic key. '
                                          'They usually start with sk-ant-. Saved anyway.')})


@tenant_admin_and_reauthed
def scan_key_test(request):
    """Check a key without spending anything on it.

    Uses models.retrieve on the exact model run_scan names — the Models API is
    GA, needs no beta header and bills no tokens, so this button is free to press
    as often as an organiser likes. retrieve rather than list because it also
    catches "valid key, but no access to this model", a real failure mode that
    otherwise first appears as a failed scan at the venue.

    Deliberately reports "Key accepted", not "Ready to scan": it proves
    authentication and model access, and says nothing about the rate limits a
    brand-new account will hit on the first round-end burst.
    """
    _require_scan_enabled()
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    tenant = get_tenant(request)
    if tenant is None:
        return _bad('No tournament.')

    key = (request.POST.get('api_key') or '').strip()
    if not key:
        # Test the stored key when the field is left blank, so an unchanged
        # credential can be re-tested without re-typing it.
        key = scan_key.resolve_key(tenant.subdomain)
    if not key:
        return _bad('Enter a key first.')

    try:
        import anthropic
    except ImportError:
        # The scanning stack is optional (see views.scan): on a host without it
        # this must be a sentence, not a 500.
        return _bad("This server does not have the scanning software installed, so "
                    "the key cannot be checked here.")

    logger.info("scan key test by %s (tenant %s), key ending %s",
                request.user, tenant.subdomain, key[-4:])
    try:
        client = anthropic.Anthropic(api_key=key, timeout=10.0, max_retries=0)
        client.models.retrieve(OCR_MODEL)
    except anthropic.AuthenticationError:
        return _bad('That key was rejected. Check you pasted all of it.')
    except anthropic.PermissionDeniedError:
        return _bad('That key works, but it cannot use the model that scanning needs.')
    except anthropic.NotFoundError:
        return _bad(f'This key has no access to {OCR_MODEL}.')
    except Exception:
        logger.exception("scan key test failed for tenant %s", tenant.subdomain)
        return _bad('Could not reach the API to check the key. Try again in a moment.')
    return JsonResponse({'status': 'ok', 'message': 'Key accepted.'})


def _parse_bbox(post, width, height):
    """The crop rectangle, validated against the image it was drawn on.

    Returns (bbox, error). A box outside the sheet, or an inside-out one, would
    produce an empty or nonsense crop that only shows up as unreadable OCR.
    """
    try:
        x1, y1, x2, y2 = (int(post.get(k, '')) for k in ('bbox_x1', 'bbox_y1', 'bbox_x2', 'bbox_y2'))
    except (TypeError, ValueError):
        return None, 'Draw the score-column box on the sheet first.'
    if x2 <= x1 or y2 <= y1:
        return None, 'That box is empty. Drag across the score columns.'
    if x1 < 0 or y1 < 0 or x2 > width or y2 > height:
        return None, 'That box falls outside the sheet image.'
    return (x1, y1, x2, y2), None


@tenant_admin_required
def scan_template_save(request):
    """Store the tenant's blank score sheet and the score-column crop box.

    Both together or neither: a template with no box, or a box with no template,
    reads nothing. Clearing removes the sheet, which turns scanning off — there
    is no default sheet to fall back to.
    """
    _require_scan_enabled()
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    tenant = get_tenant(request)
    if tenant is None:
        return _bad('No tournament.')
    cfg, _ = ScanConfig.objects.get_or_create(tenant=tenant)

    if request.POST.get('clear_template') == '1':
        cfg.template_img = None
        cfg.template_etag = ''
        cfg.bbox_x1 = cfg.bbox_y1 = cfg.bbox_x2 = cfg.bbox_y2 = 0
        cfg.save()
        return JsonResponse({'status': 'ok', 'has_template': False})

    upload = request.FILES.get('template')
    if upload is not None:
        if upload.size > MAX_TEMPLATE_BYTES:
            mb = MAX_TEMPLATE_BYTES // (1024 * 1024)
            return _bad(f'That image is too large (limit {mb} MB).')
        raw = upload.read()
        try:
            import cv2
            import numpy as np
        except ImportError:
            return _bad("This server does not have the scanning software installed, so "
                        "the sheet cannot be prepared here.")
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return _bad('That file is not an image the app can read.')
        height, width = image.shape[:2]
        bbox, err = _parse_bbox(request.POST, width, height)
        if err:
            return _bad(err)
        # Re-encode rather than storing what was uploaded: a 20 MB phone PNG would
        # otherwise sit in a row the worker reads on every job.
        ok, buf = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return _bad('That image could not be prepared.')
        stored = buf.tobytes()
        cfg.template_img = stored
        cfg.template_etag = hashlib.md5(stored).hexdigest()
    else:
        # No new file: this is a box adjustment on the sheet already stored.
        if not cfg.template_img:
            return _bad('Upload a picture of your blank score sheet first.')
        try:
            import cv2
            import numpy as np
            image = cv2.imdecode(np.frombuffer(bytes(cfg.template_img), np.uint8),
                                 cv2.IMREAD_COLOR)
        except ImportError:
            return _bad("This server does not have the scanning software installed.")
        if image is None:
            return _bad('The stored sheet image could not be read. Upload it again.')
        height, width = image.shape[:2]
        bbox, err = _parse_bbox(request.POST, width, height)
        if err:
            return _bad(err)

    cfg.bbox_x1, cfg.bbox_y1, cfg.bbox_x2, cfg.bbox_y2 = bbox
    cfg.save()
    logger.info("scan template saved by %s (tenant %s), etag %s bbox %s",
                request.user, tenant.subdomain, cfg.template_etag, bbox)
    return JsonResponse({'status': 'ok', 'has_template': True,
                         'etag': cfg.template_etag, 'bbox': list(bbox)})


@tenant_admin_required
def scan_template_preview(request):
    """Queue a rehearsal: a photographed sheet in, and either the crop or a read.

    Two modes, one path. `Test alignment` runs run_preview — no API client is
    ever constructed, so it is free and can be pressed until the crop box is
    right. `Test scan` (`full=1`) runs the real thing: the same OCR call a player
    buys, billed to this tournament's key, with the numbers handed back for the
    organiser to compare against the sheet in their hand.

    The second one is the only check that covers the whole path. A crop can be
    pixel-perfect and still read as nonsense, because the prompt assumes 16 hands
    in Value / Winner / Discarder columns — so a structurally different sheet
    passes the alignment test and fails at the venue. Nothing is written to any
    score sheet either way: this reads, it does not fill anything in.
    """
    _require_scan_enabled()
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    tenant = get_tenant(request)
    if tenant is None:
        return _bad('No tournament.')
    from .. import scan_queue
    full = request.POST.get('full') == '1'
    upload = request.FILES.get('photo')
    if upload is None:
        return _bad('Take or choose a photo of a filled sheet first.')
    if upload.size > MAX_TEMPLATE_BYTES:
        mb = MAX_TEMPLATE_BYTES // (1024 * 1024)
        return _bad(f'That photo is too large (limit {mb} MB).')
    if full and not scan_key.resolve_key(tenant.subdomain):
        # This one spends money, so it needs the same thing a real scan needs.
        return _bad('Save an API key first. A test scan is a real read, and it is billed.')
    try:
        scan_queue.sweep_stale_images()
        job_id = scan_queue.stage_image(upload.read())
        scan_queue.enqueue({
            'job_id': job_id,
            'round_nb': None,
            'table_nb': None,
            'subdomain': tenant.subdomain,
            'preview': not full,
            'rehearsal': True,
        })
    except Exception:
        logger.exception("could not queue a scanning rehearsal")
        return _bad('Scan service is unavailable. Try again.', status=503)
    return JsonResponse({'status': 'ok', 'job_id': job_id})


@tenant_admin_required
def scan_template_preview_status(request):
    """Poll an alignment test.

    Its own endpoint rather than scan_status: that one is anonymous and returns
    only scores, and the crop image has no business travelling through a public
    poller. Tenant-scoped the same way, so one tournament cannot read another's.
    """
    _require_scan_enabled()
    from .. import scan_queue
    tenant = get_tenant(request)
    job_id = request.GET.get('job_id', '')
    if not job_id or tenant is None:
        return _bad('job_id required')
    result = scan_queue.get_result(job_id)
    if result is None:
        return JsonResponse({'status': 'expired',
                             'error': 'The test timed out. Try again.'})
    if (result.get('subdomain') or '') != tenant.subdomain:
        return JsonResponse({'status': 'expired',
                             'error': 'The test timed out. Try again.'})
    if result.get('status') == 'pending':
        return JsonResponse({'status': 'pending'})
    if result.get('status') == 'done':
        return JsonResponse({'status': 'ok', 'preview': result.get('preview', ''),
                             'scores': result.get('scores', [])})
    return _bad(result.get('error', 'The photo could not be aligned.'))


def _page_scanning(request, tenant, error=None):
    """Render the Scanning page.

    Exposes whether a key is set and how it ends — never the ciphertext, never
    the key. `key_readable` is False when a stored key no longer decrypts, which
    means DJANGO_SECRET_KEY was rotated: without saying so the page would show
    "configured" for a key nothing can use.
    """
    from django.template import loader

    cfg = ScanConfig.objects.filter(tenant=tenant).order_by('id').first()
    has_key = bool(cfg and cfg.api_key_enc)
    has_template = bool(cfg and cfg.has_template)
    template2 = loader.get_template('mahj/admin_scanning.html')
    return template2.render({
        "has_key": has_key,
        "key_tail": cfg.key_tail if cfg else '',
        "key_readable": bool(scan_key.resolve_key(tenant.subdomain)) if has_key else True,
        "last_error": cfg.last_error if cfg else '',
        "last_error_at": cfg.last_error_at if cfg else None,
        "has_template": has_template,
        # Both halves or no scanning, so say which one is still missing rather
        # than leaving an organiser to work out why the QR never appeared.
        "can_scan": has_key and has_template,
        "template_etag": cfg.template_etag if cfg else '',
        "bbox": list(cfg.bbox) if (cfg and cfg.bbox) else [],
        "example_bbox": list(EXAMPLE_SHEET_BBOX),
        "subdomain": tenant.subdomain if tenant else '',
    }, request)


@tenant_admin_required
def scan_template_image(request):
    """Serve this tenant's stored sheet back to its own admin page.

    Tenant-scoped and admin-only: it is not a secret, but it is not public
    either, and the page needs it as an <img> to draw the crop box on.
    """
    _require_scan_enabled()
    tenant = get_tenant(request)
    if tenant is None:
        raise Http404
    cfg = ScanConfig.objects.filter(tenant=tenant).order_by('id').first()
    if cfg is None or not cfg.template_img:
        raise Http404
    response = HttpResponse(bytes(cfg.template_img), content_type='image/jpeg')
    # Content-addressed: the etag changes with the bytes, so the page can cache
    # hard and still show a re-upload immediately.
    response['ETag'] = f'"{cfg.template_etag}"'
    response['Cache-Control'] = 'private, max-age=60'
    return response
