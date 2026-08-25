"""Per-tenant backup endpoints: download a tournament dump, restore one.

The heavy lifting is mahj.tenant_dump; these views add the HTTP contract —
tenant-admin gating, the typed-confirmation check and operator-readable
errors. The Backup & restore page (admin?page=backup) is the front end, and
publish.trigger uploads the same dump automatically on every web publish.
"""
import logging

from django.http import HttpResponse, JsonResponse

from ..tenant_dump import (
    TenantDumpError, dump_filename, dump_tenant, parse_dump, restore_tenant,
)
from .helpers import get_tenant, tenant_admin_required
from .user_admin import tenant_admin_and_reauthed

logger = logging.getLogger(__name__)

# An uploaded dump is held in memory while it parses. A real one is a few MB at
# most (the logo dominates), so anything bigger is not a dump.
MAX_DUMP_BYTES = 50 * 1024 * 1024


@tenant_admin_required
def tenant_dump_download(request):
    """Download this tenant's whole tournament as a .json.gz dump."""
    tenant = get_tenant(request)
    if tenant is None:
        return JsonResponse({'status': 'error', 'error': 'No tournament.'}, status=400)
    data = dump_tenant(tenant)
    response = HttpResponse(data, content_type='application/gzip')
    response['Content-Disposition'] = (
        f'attachment; filename="{dump_filename(tenant.subdomain)}"')
    return response


@tenant_admin_and_reauthed
def tenant_restore(request):
    """Replace this tenant's tournament with an uploaded dump.

    parse_dump validates the file before anything is deleted, and the wipe+load
    share one transaction, so every failure leaves the tournament as it was.
    Publish config and memberships are never touched (see mahj.tenant_dump).
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    tenant = get_tenant(request)
    if tenant is None:
        return JsonResponse({'status': 'error', 'error': 'No tournament.'}, status=400)
    # Retype-the-subdomain confirmation, the same habit as tenant deletion: the
    # modal collects it, the server enforces it.
    if (request.POST.get('confirm') or '').strip() != tenant.subdomain:
        return JsonResponse(
            {'status': 'error',
             'error': "Confirmation text does not match this tournament's subdomain."},
            status=400)
    upload = request.FILES.get('dumpfile')
    if upload is None:
        return JsonResponse({'status': 'error', 'error': 'No file uploaded.'}, status=400)
    if upload.size > MAX_DUMP_BYTES:
        return JsonResponse(
            {'status': 'error', 'error': 'That file is too large to be a tournament dump.'},
            status=400)
    try:
        payload = parse_dump(upload.read())
    except TenantDumpError as exc:
        return JsonResponse({'status': 'error', 'error': str(exc)}, status=400)
    try:
        result = restore_tenant(tenant, payload)
    except Exception:
        logger.exception("Tenant restore failed for %r", tenant.subdomain)
        return JsonResponse(
            {'status': 'error',
             'error': 'Restore failed — the tournament was left unchanged. '
                      'Check the server log.'},
            status=500)
    logger.info("Tenant %r restored from a dump of %r by %s (%d rows)",
                tenant.subdomain, result['source_subdomain'],
                request.user.username, sum(result['counts'].values()))
    return JsonResponse({'status': 'ok', **result})
