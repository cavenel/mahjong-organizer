"""Upload an exported static site to a plain web host over SFTP.

Each tenant's target is resolved by `resolve_config`, which prefers a
staff-configured per-tenant target stored in the DB (`PublishTarget`) and falls
back to process env vars — so a single-tenant install still works with just
`.env`, while a multi-tenant install can publish each tenant to its own host:

    PUBLISH_SFTP_HOST         target host (unset → env fallback disabled)
    PUBLISH_SFTP_PORT         default 22
    PUBLISH_SFTP_USER         ssh user
    PUBLISH_SFTP_PASSWORD     password auth (optional)
    PUBLISH_SFTP_KEY          path to a private key (optional; preferred)
    PUBLISH_SFTP_PATH         remote directory the site is served from
    PUBLISH_SFTP_KNOWN_HOSTS  known_hosts file (optional; else auto-add)
    PUBLISH_TENANT            restrict the ENV fallback to one tenant — guards a
                              shared env target against a test tenant clobbering
                              the live site. Per-tenant DB targets ignore it
                              (each tenant either has its own target or doesn't).

`version.json` is uploaded last so a polling client never sees a new version
before the files it points at have landed.
"""
import io
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import paramiko

logger = logging.getLogger(__name__)

# Uploaded last, on its own, after every other file is in place.
VERSION_FILE = 'version.json'

# Local record of what we last uploaded (relpath → [size, mtime]) so repeat
# publishes skip unchanged files — the CSS/JS/flags don't change between rounds,
# so only the regenerated HTML + version.json actually upload. Kept in the export
# dir, never uploaded.
MANIFEST_FILE = '.upload_manifest.json'

# Env config is read live from os.environ inside the resolver below (never cached
# at import) so the standalone launcher and tests can set it after import.


@dataclass
class PublishConfig:
    """The effective SFTP target for one tenant, from a DB target or env."""
    host: str
    port: int
    username: str
    path: str          # remote directory the site is served from
    password: str = ''
    key_path: str = ''      # path to a private key file (env fallback)
    key_data: str = ''      # inline private key PEM (DB target)
    known_hosts_file: str = ''  # a known_hosts file to load + pin (env fallback)
    host_key: str = ''      # a single known_hosts line to pin (DB target)


def _env_config(subdomain):
    """Config from PUBLISH_SFTP_* env, honouring the PUBLISH_TENANT gate. None if
    no host is set, or PUBLISH_TENANT is set and `subdomain` doesn't match it."""
    host = os.environ.get('PUBLISH_SFTP_HOST', '')
    if not host:
        return None
    gate = os.environ.get('PUBLISH_TENANT', '')
    if gate and subdomain != gate:
        return None
    return PublishConfig(
        host=host,
        port=int(os.environ.get('PUBLISH_SFTP_PORT', '22') or '22'),
        username=os.environ.get('PUBLISH_SFTP_USER', ''),
        path=os.environ.get('PUBLISH_SFTP_PATH', '') or '.',
        password=os.environ.get('PUBLISH_SFTP_PASSWORD', ''),
        key_path=os.environ.get('PUBLISH_SFTP_KEY', ''),
        known_hosts_file=os.environ.get('PUBLISH_SFTP_KNOWN_HOSTS', ''),
    )


def _db_config(subdomain):
    """Config from an enabled PublishTarget for `subdomain`, or None."""
    if not subdomain:
        return None
    from ..models import PublishTarget
    from . import secrets
    target = (PublishTarget.objects
              .filter(tenant__subdomain=subdomain, enabled=True, host__gt='')
              .order_by('id').first())
    if target is None:
        return None
    return PublishConfig(
        host=target.host,
        port=target.port or 22,
        username=target.username,
        path=target.path or '.',
        password=secrets.decrypt(target.password_enc),
        key_data=secrets.decrypt(target.private_key_enc),
        host_key=target.host_key or '',
    )


def resolve_config(subdomain):
    """The effective publish config for a tenant: an enabled DB PublishTarget
    wins, else the PUBLISH_SFTP_* env fallback, else None (publishing off)."""
    return _db_config(subdomain) or _env_config(subdomain)


def is_configured(subdomain=None):
    """True if this tenant has an effective publish config (DB target or env)."""
    return resolve_config(subdomain) is not None


def _load_private_key(pem):
    """Parse an inline PEM private key, trying each supported key type."""
    for loader in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return loader.from_private_key(io.StringIO(pem))
        except paramiko.SSHException:
            continue
    raise paramiko.SSHException("Unsupported or invalid private key.")


def _pin_host_key(client, line):
    """Pin a single known_hosts line onto `client` (the DB target's host_key)."""
    from paramiko.hostkeys import HostKeyEntry
    entry = HostKeyEntry.from_line(line)
    if entry is None:
        raise ValueError("Host key is not a valid known_hosts line.")
    for name in entry.hostnames:
        client.get_host_keys().add(name, entry.key.get_name(), entry.key)


def _connect(cfg):
    client = paramiko.SSHClient()
    if cfg.known_hosts_file:
        client.load_host_keys(cfg.known_hosts_file)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    elif cfg.host_key:
        _pin_host_key(client, cfg.host_key)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        # No host key pinned: accept it. Acceptable for a staff-configured
        # target; set a known_hosts file (env) or host_key line (DB) to pin it.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=cfg.host,
        port=cfg.port,
        username=cfg.username or None,
        password=cfg.password or None,
        key_filename=cfg.key_path or None,
        pkey=_load_private_key(cfg.key_data) if cfg.key_data else None,
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


def upload_dir(local_dir, subdomain=None, full=False, progress=None):
    """Upload `local_dir` to the tenant's remote path, skipping files unchanged
    since the last upload (unless `full`). version.json goes last.

    `progress`, if given, is called as progress(done, total) after each file, so
    the caller can drive a progress bar. Does nothing if the tenant has no
    resolved publish target (DB or env).
    """
    cfg = resolve_config(subdomain)
    if cfg is None:
        logger.info("SFTP upload skipped: no publish target for %r.", subdomain)
        return

    local_dir = Path(local_dir)
    remote_root = (cfg.path or '.').rstrip('/')

    manifest_path = local_dir / MANIFEST_FILE
    previous = {}
    if not full and manifest_path.exists():
        try:
            previous = json.loads(manifest_path.read_text())
        except (ValueError, OSError):
            previous = {}

    # Everything except the local-only manifest; version.json last so a client
    # never polls a new version before the files it points at have landed.
    files = [p for p in local_dir.rglob('*') if p.is_file() and p.name != MANIFEST_FILE]
    files.sort(key=lambda p: (p.name == VERSION_FILE, str(p)))

    # Skip files whose size+mtime match the last upload (static assets rarely
    # change); always send version.json so the refresh signal lands.
    manifest = {}
    to_send = []
    for f in files:
        rel = f.relative_to(local_dir).as_posix()
        st = f.stat()
        sig = [st.st_size, int(st.st_mtime)]
        manifest[rel] = sig
        if f.name == VERSION_FILE or previous.get(rel) != sig:
            to_send.append((f, rel))

    total = len(to_send)
    client = _connect(cfg)
    try:
        sftp = client.open_sftp()
        made = set()
        for i, (f, rel) in enumerate(to_send, start=1):
            remote = f'{remote_root}/{rel}'
            rdir = remote.rsplit('/', 1)[0]
            if rdir not in made:
                _ensure_remote_dir(sftp, rdir)
                made.add(rdir)
            sftp.put(str(f), remote)
            if progress:
                progress(i, total)
        sftp.close()
    finally:
        client.close()

    try:
        manifest_path.write_text(json.dumps(manifest))
    except OSError:
        pass  # manifest is an optimization; losing it just means a full next upload
    logger.info("Uploaded %d of %d files for %r to %s (skipped %d unchanged)",
                len(to_send), len(files), subdomain, remote_root, len(files) - len(to_send))
