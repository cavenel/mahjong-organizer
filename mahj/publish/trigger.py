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

logger = logging.getLogger(__name__)

# Serialize exports so two close-together publishes can't interleave writes into
# the same output dir.
_export_lock = threading.Lock()


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
            export_public(subdomain, out)
            upload_dir(out, subdomain=subdomain)
            logger.info("Static site published for %r", subdomain)
        except Exception as e:
            logger.warning("Static publish failed for %r: %s", subdomain, e)


def fire_static_export(subdomain):
    """Render + upload the public static site in a background thread (or no-op)."""
    if not _should_publish(subdomain):
        return
    threading.Thread(target=_run, args=(subdomain,), daemon=True).start()
