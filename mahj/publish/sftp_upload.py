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
import re
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

# Default directory for the tournament dumps uploaded on every publish, and how
# many to keep. Placed BESIDE the served site, never inside it: a dump holds every
# score, including a withheld final round the public site is still hiding, so a
# guessable URL under the web root would leak the podium before the ceremony.
DUMP_DIR_NAME = 'mahj-backups'
KEEP_DUMPS = 20


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
    subdomain: str = ''     # so a learned host key can be written back to the row
    backup_path: str = ''   # where dumps go; blank → beside the site (see dump_dir)

    def dump_dir(self):
        """The remote directory tournament dumps are uploaded to.

        The operator's ``backup_path`` wins. Otherwise a sibling of the site
        directory (``public_html`` → ``mahj-backups``), so the default is not
        web-fetchable; with no site path to be a sibling of, it is relative to
        the login directory, which is not served either.
        """
        if self.backup_path:
            return self.backup_path.rstrip('/')
        site = (self.path or '').rstrip('/')
        parent = site.rsplit('/', 1)[0] if '/' in site else ''
        return f'{parent}/{DUMP_DIR_NAME}' if parent else DUMP_DIR_NAME


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
        subdomain=subdomain,
        backup_path=(target.backup_path or '').strip(),
    )


def is_configured(subdomain=None):
    """True if this tenant has an enabled publish target."""
    return resolve_config(subdomain) is not None


def _load_private_key(pem):
    """Parse an inline PEM private key, trying each supported key type."""
    encrypted = False
    for loader in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return loader.from_private_key(io.StringIO(pem))
        except paramiko.PasswordRequiredException:
            # A key of a type we support, just locked. There is nowhere to type a
            # passphrase (the key is stored, not typed per publish), so say that
            # rather than let it fall through as "unsupported or invalid" — it
            # subclasses SSHException, so it used to.
            encrypted = True
        except paramiko.SSHException:
            continue
    if encrypted:
        raise paramiko.SSHException(
            "This key is passphrase-protected; paste an unencrypted key.")
    raise paramiko.SSHException("Unsupported or invalid private key.")


def _pin_host_key(client, line):
    """Pin a single known_hosts line onto `client` (the DB target's host_key)."""
    from paramiko.hostkeys import HostKeyEntry
    entry = HostKeyEntry.from_line(line)
    if entry is None:
        raise ValueError("Host key is not a valid known_hosts line.")
    for name in entry.hostnames:
        client.get_host_keys().add(name, entry.key.get_name(), entry.key)


def _known_hosts_name(cfg):
    """How a known_hosts line names this target — bracketed for a non-22 port,
    matching both paramiko and OpenSSH."""
    return cfg.host if cfg.port == 22 else f'[{cfg.host}]:{cfg.port}'


def _remember_host_key(cfg, client):
    """Trust on first use: store the key we just saw so every later connect to this
    target is checked against it.

    Only fills an empty ``host_key``, and only via a filtered UPDATE, so it can
    never overwrite a key the operator pinned by hand or one a concurrent publish
    learned first. A changed key from then on is a RejectPolicy failure, which is
    the point — accepting a new key on every connect is what makes an unpinned
    target MITM-able for its whole life, not just its first second.
    """
    transport = client.get_transport()
    if transport is None or not cfg.subdomain:
        return
    key = transport.get_remote_server_key()
    line = f'{_known_hosts_name(cfg)} {key.get_name()} {key.get_base64()}'
    from ..models import PublishTarget
    written = (PublishTarget.objects
               .filter(tenant__subdomain=cfg.subdomain, host=cfg.host, host_key='')
               .update(host_key=line))
    if written:
        logger.info("Pinned the host key for %s on first connect (%s). Later connects "
                 "are verified against it; a change will be refused.",
                 cfg.host, key.get_name())


def _connect(cfg):
    client = paramiko.SSHClient()
    pinned = bool(cfg.host_key)
    if pinned:
        _pin_host_key(client, cfg.host_key)
    client.set_missing_host_key_policy(
        # Pinned: anything else is refused. Unpinned: learn it on this one connect
        # and write it back below, so the trust-on-first-use window is a single
        # connection rather than every connection.
        paramiko.RejectPolicy() if pinned else paramiko.AutoAddPolicy())
    client.connect(
        hostname=cfg.host,
        port=cfg.port,
        username=cfg.username or None,
        password=cfg.password or None,
        pkey=_load_private_key(cfg.key_data) if cfg.key_data else None,
        timeout=15,
        # Authenticate with exactly what the publish form configured, and nothing
        # else. Both of these default to True in paramiko, which means offering the
        # process's own SSH identities — the agent's keys and ~/.ssh/id_* — to
        # whatever host the organizer typed in. That host then learns which keys we
        # hold, and auth can succeed on an identity nobody configured, so the
        # target's real access stops matching what the form says. Worst on the
        # standalone laptop, where those are the organizer's personal keys.
        # configuration.md documents a password *or* a private key as the config, so
        # there is no supported setup that relied on the ambient ones.
        allow_agent=False,
        look_for_keys=False,
    )
    if not pinned:
        try:
            _remember_host_key(cfg, client)
        except Exception:
            # Never fail a publish because we couldn't record the key; the next
            # connect just learns it again.
            logger.warning("Could not store the host key for %s", cfg.host, exc_info=True)
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


def _prune_dumps(sftp, remote_dir, subdomain):
    """Keep only the newest KEEP_DUMPS dumps for this tenant in `remote_dir`.

    Matched by this tenant's own filename prefix, so tenants publishing to a
    shared directory never reap each other's dumps. The names carry a sortable
    UTC stamp (see tenant_dump.dump_filename), so lexical order is time order.
    """
    # The stamp is matched explicitly, so tenant "oemc" can never reap
    # "oemc_2026"'s dumps (its name would otherwise look like this one's prefix).
    pattern = re.compile(rf'^mahj_{re.escape(subdomain)}_\d{{8}}T\d{{6}}Z\.json\.gz$')
    try:
        names = sorted(n for n in sftp.listdir(remote_dir) if pattern.match(n))
    except IOError:
        return
    for name in names[:-KEEP_DUMPS] if len(names) > KEEP_DUMPS else []:
        try:
            sftp.remove(f'{remote_dir}/{name}')
        except IOError:
            logger.warning("Could not remove the old dump %s", name)


def upload_dump(subdomain, data, filename):
    """Upload one tournament dump to the tenant's backup directory, then prune
    the old ones. No-op when the tenant has no enabled publish target.

    Called after a successful site publish (see publish.trigger) so every
    published state has an off-site restore point.
    """
    cfg = resolve_config(subdomain)
    if cfg is None:
        logger.info("Dump upload skipped: no publish target for %r.", subdomain)
        return
    remote_dir = cfg.dump_dir()
    client = _connect(cfg)
    try:
        sftp = client.open_sftp()
        _ensure_remote_dir(sftp, remote_dir)
        sftp.putfo(io.BytesIO(data), f'{remote_dir}/{filename}')
        _prune_dumps(sftp, remote_dir, subdomain)
        sftp.close()
    finally:
        client.close()
    logger.info("Uploaded the tournament dump %s for %r to %s (%d bytes)",
                filename, subdomain, remote_dir, len(data))
