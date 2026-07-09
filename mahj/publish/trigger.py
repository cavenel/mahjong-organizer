"""Fire a public-site export+upload after a publish, off the request thread.

Publish/unpublish regenerates the static spectator site and SFTP-uploads it. It
runs in a daemon thread so a publish request never blocks on rendering or the
network, and is a no-op unless the publishing tenant has an enabled publish
target (see publish.sftp_upload).
"""
import logging
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
# the cloud) so the shell-wide toast can show a live bar on any page. In-progress
# state lingers (in case a publish hangs); terminal state (done/error) expires
# fast so the toast auto-clears instead of re-appearing on every navigation.
_PROGRESS_TTL = 300
_TERMINAL_TTL = 20


def _progress_key(subdomain):
    return f'publish_progress:{subdomain}'


def set_progress(subdomain, phase, pct=None, message='', error=''):
    ttl = _TERMINAL_TTL if phase in ('done', 'error') else _PROGRESS_TTL
    cache.set(_progress_key(subdomain),
              {'phase': phase, 'pct': pct, 'message': message, 'error': error},
              ttl)


def get_progress(subdomain):
    return cache.get(_progress_key(subdomain))


def _should_publish(subdomain):
    from .sftp_upload import is_configured
    return is_configured(subdomain)


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
