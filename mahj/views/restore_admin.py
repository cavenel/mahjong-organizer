"""Snapshot restore for the standalone build.

The launcher (standalone/run.py) takes rolling sqlite snapshots of the local
database; this endpoint schedules one to be swapped in on the next launch, since
a single process can't safely replace the sqlite file it holds open. The listing
and the swap itself live in ``mahj.standalone_backup``.

Cloud installs restore per tenant instead, from a tournament dump — see
``mahj.tenant_dump`` and the Backup & restore admin page. There is no
whole-cluster restore console: a Postgres deploy is restored by its host's own
snapshots, and a tournament by its dump.
"""
from django.conf import settings
from django.http import JsonResponse

from .helpers import json_body
from .user_admin import superuser_and_reauthed


@superuser_and_reauthed
def restore_run(request):
    """Schedule a local sqlite snapshot to be restored on the next launch.

    Gated by a typed confirmation (``standalone_backup.CONFIRM_TOKEN``), like
    every other irreversible wipe in the console.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    if not settings.STANDALONE:
        # Snapshots are a standalone-build mechanism; a cloud install restores a
        # tournament dump instead (views/backup_admin.py).
        return JsonResponse({'status': 'error', 'error': 'Not available in this build.'},
                            status=404)
    from .. import standalone_backup
    data = json_body(request)
    if (data.get('confirm') or '') != standalone_backup.CONFIRM_TOKEN:
        return JsonResponse(
            {'status': 'error', 'error': f"Type '{standalone_backup.CONFIRM_TOKEN}' to confirm."},
            status=400)
    if not standalone_backup.request_restore(data.get('dump') or ''):
        return JsonResponse({'status': 'error', 'error': 'Unknown or invalid snapshot.'},
                            status=400)
    return JsonResponse({'status': 'ok', 'standalone': True,
                         'message': 'Restore scheduled. Quit and relaunch the app to apply it.'})
