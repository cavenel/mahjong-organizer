"""Upload an exported static site to a plain web host over SFTP.

Configured entirely via env vars (loaded from .env by docker-compose in the
robust profile, or by the standalone launcher):

    PUBLISH_SFTP_HOST         target host (unset → uploads are disabled)
    PUBLISH_SFTP_PORT         default 22
    PUBLISH_SFTP_USER         ssh user
    PUBLISH_SFTP_PASSWORD     password auth (optional)
    PUBLISH_SFTP_KEY          path to a private key (optional; preferred)
    PUBLISH_SFTP_PATH         remote directory the site is served from
    PUBLISH_SFTP_KNOWN_HOSTS  known_hosts file (optional; else auto-add)
    PUBLISH_TENANT            only this tenant's exports upload (mirrors
                              WEBHOOK_TENANT) — guards a shared target against a
                              test tenant clobbering the live site.

`version.json` is uploaded last so a polling client never sees a new version
before the files it points at have landed.
"""
import logging
import os
from pathlib import Path

import paramiko

logger = logging.getLogger(__name__)

# Uploaded last, on its own, after every other file is in place.
VERSION_FILE = 'version.json'

# All config is read live from os.environ inside the functions below (never
# cached at import) so the standalone launcher and tests can set it after import.


def is_configured():
    """True if a target host is set. Read live so tests/launcher can set env late."""
    return bool(os.environ.get('PUBLISH_SFTP_HOST', ''))


def _connect():
    client = paramiko.SSHClient()
    known = os.environ.get('PUBLISH_SFTP_KNOWN_HOSTS', '')
    if known:
        client.load_host_keys(known)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        # No known_hosts pinned: accept the host key. Acceptable for a controlled
        # publish target; set PUBLISH_SFTP_KNOWN_HOSTS to pin it instead.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=os.environ.get('PUBLISH_SFTP_HOST', ''),
        port=int(os.environ.get('PUBLISH_SFTP_PORT', '22') or '22'),
        username=os.environ.get('PUBLISH_SFTP_USER', '') or None,
        password=os.environ.get('PUBLISH_SFTP_PASSWORD', '') or None,
        key_filename=os.environ.get('PUBLISH_SFTP_KEY', '') or None,
        timeout=15,
    )
    return client


def _ensure_remote_dir(sftp, remote_dir):
    """mkdir -p `remote_dir` on the far side, creating each parent in turn."""
    absolute = remote_dir.startswith('/')
    cur = ''
    for part in (p for p in remote_dir.split('/') if p):
        cur = f'{cur}/{part}' if cur else (f'/{part}' if absolute else part)
        try:
            sftp.stat(cur)
        except IOError:
            sftp.mkdir(cur)


def upload_dir(local_dir, subdomain=None):
    """Upload everything under `local_dir` to PUBLISH_SFTP_PATH.

    Does nothing if unconfigured, or if PUBLISH_TENANT is set and `subdomain`
    doesn't match it. version.json goes last.
    """
    if not is_configured():
        logger.info("SFTP upload skipped: PUBLISH_SFTP_HOST not set.")
        return

    gate = os.environ.get('PUBLISH_TENANT', '')
    if gate and subdomain != gate:
        logger.info("SFTP upload skipped: tenant %r is not PUBLISH_TENANT %r.",
                    subdomain, gate)
        return

    local_dir = Path(local_dir)
    remote_root = (os.environ.get('PUBLISH_SFTP_PATH', '') or '.').rstrip('/')

    # All files except version.json, then version.json last.
    files = [p for p in local_dir.rglob('*') if p.is_file()]
    files.sort(key=lambda p: (p.name == VERSION_FILE, str(p)))

    client = _connect()
    try:
        sftp = client.open_sftp()
        made = set()
        for f in files:
            rel = f.relative_to(local_dir).as_posix()
            remote = f'{remote_root}/{rel}'
            rdir = remote.rsplit('/', 1)[0]
            if rdir not in made:
                _ensure_remote_dir(sftp, rdir)
                made.add(rdir)
            sftp.put(str(f), remote)
        sftp.close()
    finally:
        client.close()
    logger.info("Uploaded %d files for %r to %s", len(files), subdomain, remote_root)
