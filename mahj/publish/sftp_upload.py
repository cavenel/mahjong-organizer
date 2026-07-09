"""Upload an exported static site to a plain web host over SFTP.

Each tenant's target is a staff-configured `PublishTarget` row (host, port,
user, remote path, and a password or private key), resolved by `resolve_config`.
A multi-tenant install publishes each tenant to its own host; a tenant with no
enabled target simply doesn't publish. Configure targets in the admin console.

`version.json` is uploaded last so a polling client never sees a new version
before the files it points at have landed.
"""
import io
import json
import logging
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


@dataclass
class PublishConfig:
    """The SFTP target for one tenant, resolved from its PublishTarget row."""
    host: str
    port: int
    username: str
    path: str          # remote directory the site is served from
    password: str = ''
    key_data: str = ''      # inline private key PEM
    host_key: str = ''      # a single known_hosts line to pin the host (optional)


def resolve_config(subdomain):
    """The publish config for a tenant: its enabled PublishTarget, or None."""
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


def is_configured(subdomain=None):
    """True if this tenant has an enabled publish target."""
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
    if cfg.host_key:
        _pin_host_key(client, cfg.host_key)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        # No host key pinned: accept it on first connect. Acceptable for a
        # staff-configured target; set a host_key line to pin it instead.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=cfg.host,
        port=cfg.port,
        username=cfg.username or None,
        password=cfg.password or None,
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
