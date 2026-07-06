"""Fire a public-site export+upload after a publish, off the request thread.

Mirrors webhook.fire_webhook: publish/unpublish already pushes leaderboard JSON
to the optional webhook; this does the parallel job of regenerating the static
spectator site and SFTP-uploading it. It runs in a daemon thread so a publish
request never blocks on rendering or the network, and is a no-op unless SFTP is
configured (PUBLISH_SFTP_HOST) and — if PUBLISH_TENANT is set — the publishing
tenant matches it.
"""
import logging
import os
import threading
from pathlib import Path

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Serialize exports so two close-together publishes can't interleave writes into
# the same output dir.
_export_lock = threading.Lock()

# Progress is written to the cache (shared between the daemon thread and the
# admin poll requests: LocMem in the single-process standalone build, Redis in
# the cloud) so the "Publish to web" button can show a live bar. Short TTL — it's
# only meaningful while a publish is running or just finished.
_PROGRESS_TTL = 300


def _progress_key(subdomain):
    return f'publish_progress:{subdomain}'


def set_progress(subdomain, phase, pct=None, message='', error=''):
    cache.set(_progress_key(subdomain),
              {'phase': phase, 'pct': pct, 'message': message, 'error': error},
              _PROGRESS_TTL)


def get_progress(subdomain):
    return cache.get(_progress_key(subdomain))


def _should_publish(subdomain):
    from .sftp_upload import is_configured
    if not is_configured():
        return False
    gate = os.environ.get('PUBLISH_TENANT', '')
    if gate and subdomain != gate:
        return False
    return True


def _run(subdomain):
    from .sftp_upload import upload_dir
    from .static_export import export_public
    out = Path(settings.BASE_DIR) / 'captures' / 'export' / subdomain
    with _export_lock:
        try:
            set_progress(subdomain, 'render', pct=5, message='Rendering pages…')
            export_public(subdomain, out)

            def _on_upload(done, total):
                pct = 100 if not total else int(done * 100 / total)
                set_progress(subdomain, 'upload', pct=pct,
                             message=f'Uploading… {done}/{total}')

            set_progress(subdomain, 'upload', pct=0, message='Uploading…')
            upload_dir(out, subdomain=subdomain, progress=_on_upload)
            set_progress(subdomain, 'done', pct=100, message='Published to the web.')
            logger.info("Static site published for %r", subdomain)
        except Exception as e:
            logger.warning("Static publish failed for %r: %s", subdomain, e)
            set_progress(subdomain, 'error', error=str(e))


def fire_static_export(subdomain):
    """Render + upload the public static site in a background thread (or no-op)."""
    if not _should_publish(subdomain):
        return
    set_progress(subdomain, 'starting', pct=0, message='Starting…')
    threading.Thread(target=_run, args=(subdomain,), daemon=True).start()
