"""One tournament in one file: per-tenant dump and restore.

A dump is a gzipped JSON snapshot of every row a tournament owns — settings
(identity, format, round timer, logo), players, seating, hands, score sheets,
published/withheld rounds, schedule, screens, screen modes and ceremony state.
It is the disaster-recovery unit: one is uploaded off the served tree on
every publish (see publish.trigger), and the Backup & restore admin page can
download one on demand and restore one into any tenant on any install —
Postgres cloud or sqlite standalone — because rows are plain field values with
no cross-model foreign keys (seating references players by draw_number value,
so nothing needs id remapping).

Deliberately NOT in a dump:
  - PublishTarget: deployment config, not tournament data. Its secrets are
    Fernet ciphertext under this install's SECRET_KEY (undecryptable anywhere
    else), and a restore must not clobber the target tenant's working publish
    setup. A dump therefore contains no secrets.
  - Membership / users: accounts are global; a dump is restored into a tenant
    the operator already administers.

A restore is all-or-nothing: the wipe and the load share one transaction, so a
constraint error rolls the tenant back to its pre-restore state.
"""
import base64
import gzip
import io
import json
from datetime import datetime, timezone

from django.db import transaction
from django.db.models import BinaryField, DateTimeField
from django.utils.dateparse import parse_datetime

from .models import (
    CeremonyState, Hand, Player, PublishTarget, PublishedRound, Schedule,
    ScoreSheet, Screen, ScreenMode, Seat, TournamentSettings,
)

# Bumped when the file layout itself changes (not on schema changes — those are
# covered by the migration stamp below).
FORMAT = 1

# Everything a tournament owns, in wipe order — also the dump/restore order.
# TournamentSettings goes last so its post_delete signal (settings-cache bust +
# display refresh) fires after the rest of the wipe. admin_reset shares this
# list via wipe_tenant, so the "what is a tournament" answer lives here once.
TENANT_MODELS = (Hand, ScoreSheet, Seat, PublishedRound, Player, Schedule,
                 Screen, ScreenMode, CeremonyState, TournamentSettings)

# Ceiling on what an uploaded dump may inflate to. A real one is a few MB even
# with a logo; this is generous enough never to refuse a genuine file and small
# enough that a gzip bomb inside the request cap can't exhaust memory.
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
_GUNZIP_CHUNK = 1024 * 1024


class TenantDumpError(Exception):
    """A problem with an uploaded dump the operator can act on (not a dump file,
    made on a different app version, corrupt content). The message is shown to
    them verbatim; any other exception is an unexpected error."""


def wipe_tenant(tenant, include_publish_target=False):
    """Delete every tournament row for `tenant`, inside the caller's transaction.

    With `include_publish_target` the publish config goes too — that is the
    full reset-page wipe. A dump restore leaves it out: the target tenant keeps
    its own working publish setup.
    """
    for model in TENANT_MODELS:
        if model is TournamentSettings and include_publish_target:
            PublishTarget.objects.filter(tenant=tenant).delete()
        model.objects.filter(tenant=tenant).delete()


def schema_version():
    """The on-disk mahj migration leaf — the schema identity stamped into every
    dump and required to match at restore. Deploys auto-migrate on start, so
    the database always matches the code, and two installs running the same
    code accept each other's dumps."""
    from django.db.migrations.loader import MigrationLoader
    leaves = MigrationLoader(None, ignore_no_migrations=True).graph.leaf_nodes('mahj')
    return leaves[0][1] if leaves else ''


def dump_filename(subdomain):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    return f'mahj_{subdomain}_{stamp}.json.gz'


def _dump_fields(model):
    """The concrete fields a dump carries: everything except the pk and the
    tenant FK, both of which are reassigned at restore (nothing references
    either — see the module docstring)."""
    return [f for f in model._meta.concrete_fields if f.name not in ('id', 'tenant')]


def _encode(field, value):
    """A field value as JSON-safe data, by declared field type."""
    if value is None:
        return None
    if isinstance(field, BinaryField):
        # bytes() also flattens the memoryview Postgres hands back.
        return base64.b64encode(bytes(value)).decode('ascii')
    if isinstance(field, DateTimeField):
        return value.isoformat()
    return value


def _decode(field, value):
    if value is None:
        return None
    if isinstance(field, BinaryField):
        return base64.b64decode(value)
    if isinstance(field, DateTimeField):
        return parse_datetime(value)
    return value


def dump_tenant(tenant):
    """Serialize every tournament row for `tenant` into gzipped JSON bytes.

    The read runs in one transaction, but Postgres defaults to READ COMMITTED, so
    that is not a snapshot: a write landing mid-dump can be included for a model
    read after it and missing from one read before it. In practice a backup is
    taken between rounds, not mid-entry, and the restore is all-or-nothing — so
    the window is left open rather than pinned with SERIALIZABLE, which would
    make the dump able to fail against live score entry.
    """
    payload = {
        'format': FORMAT,
        'migration': schema_version(),
        'subdomain': tenant.subdomain,
        'created_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'models': {},
    }
    with transaction.atomic():
        for model in TENANT_MODELS:
            fields = _dump_fields(model)
            payload['models'][model.__name__] = [
                {f.name: _encode(f, getattr(obj, f.name)) for f in fields}
                # id order is creation order: Schedule's ordering is semantic
                # (the Nth is_round row is round N), and everything else gets a
                # stable file out of it.
                for obj in model.objects.filter(tenant=tenant).order_by('id')
            ]
    return gzip.compress(json.dumps(payload).encode('utf-8'))


def _gunzip_capped(data):
    """Decompress `data`, refusing anything that expands past MAX_UNCOMPRESSED_BYTES.

    gzip.decompress() inflates the whole stream into memory before anyone can look
    at it, and gzip reaches ratios around 1000:1 — so an upload already inside the
    50 MB request cap can ask for tens of gigabytes. Reading in chunks and stopping
    at the cap keeps the refusal cheap. Reaching the cap is a rejection, not a
    truncation: a real dump is orders of magnitude smaller.
    """
    out = bytearray()
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as fh:
        while True:
            chunk = fh.read(_GUNZIP_CHUNK)
            if not chunk:
                return bytes(out)
            out += chunk
            if len(out) > MAX_UNCOMPRESSED_BYTES:
                raise TenantDumpError(
                    "That file expands to far more than a tournament dump ever "
                    "does; it was not read.")


def parse_dump(data):
    """Gunzip, parse and validate an uploaded dump — all before anything is
    deleted, so a rejected file never costs the operator their data. Returns
    the payload dict; raises TenantDumpError with a message for the operator."""
    try:
        raw = _gunzip_capped(data)
    except (OSError, EOFError):
        raise TenantDumpError("Not a tournament dump (expected a .json.gz file).")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise TenantDumpError("Not a tournament dump (the file holds no JSON).")
    if not isinstance(payload, dict) or payload.get('format') != FORMAT:
        raise TenantDumpError(f"Unsupported dump format (expected format {FORMAT}).")
    current = schema_version()
    if payload.get('migration') != current:
        raise TenantDumpError(
            f"This dump was made on a different app version (schema "
            f"{payload.get('migration') or 'unknown'!r} vs this install's "
            f"{current!r}). Restore it on a matching install.")
    rows_by_model = payload.get('models')
    if not isinstance(rows_by_model, dict):
        raise TenantDumpError("Corrupt dump: no model data found.")
    models_by_name = {m.__name__: m for m in TENANT_MODELS}
    for name, rows in rows_by_model.items():
        model = models_by_name.get(name)
        if model is None:
            raise TenantDumpError(f"Corrupt dump: unknown model {name!r}.")
        if not isinstance(rows, list):
            raise TenantDumpError(f"Corrupt dump: bad row list for {name}.")
        allowed = {f.name for f in _dump_fields(model)}
        for row in rows:
            if not isinstance(row, dict):
                raise TenantDumpError(f"Corrupt dump: bad row in {name}.")
            unknown = set(row) - allowed
            if unknown:
                raise TenantDumpError(
                    f"Corrupt dump: unknown field {sorted(unknown)[0]!r} on {name}.")
    return payload


def _restamp_auto_now(model, fields, objs, rows):
    """Write dumped timestamps back onto auto_now/auto_now_add fields
    (Screen.time): the insert stamped them with now(), and bulk_update skips
    pre_save, so it stores the real values."""
    stale = [f for f in fields.values()
             if getattr(f, 'auto_now', False) or getattr(f, 'auto_now_add', False)]
    if not stale or not objs:
        return
    touched = False
    for obj, row in zip(objs, rows):
        for f in stale:
            if row.get(f.name) is not None:
                setattr(obj, f.name, _decode(f, row[f.name]))
                touched = True
    if touched:
        model.objects.bulk_update(objs, [f.name for f in stale])


def restore_tenant(tenant, payload):
    """Wipe `tenant` and load `payload` (from parse_dump) into it, atomically.

    Publish config and memberships are left untouched. Returns
    ``{'counts': {ModelName: rows}, 'source_subdomain': ...}`` for the UI.
    """
    counts = {}
    with transaction.atomic():
        wipe_tenant(tenant)
        for model in TENANT_MODELS:
            fields = {f.name: f for f in _dump_fields(model)}
            rows = payload['models'].get(model.__name__, [])
            objs = [
                model(tenant=tenant,
                      **{name: _decode(fields[name], value) for name, value in row.items()})
                for row in rows
            ]
            model.objects.bulk_create(objs)
            _restamp_auto_now(model, fields, objs, rows)
            counts[model.__name__] = len(objs)
    _wake_pages(tenant)
    return {'counts': counts, 'source_subdomain': payload.get('subdomain', '')}


def _wake_pages(tenant):
    """The post-restore tail, mirroring admin_reset: every cached scoring
    surface is stale and every live page (public displays, scorer grids,
    projector screens) is showing pre-restore data. bulk_create fires no
    post_save signals, so the settings cache is busted by hand."""
    from django.core.cache import cache

    from .signals import broadcast_display, broadcast_publish_state, invalidate_leaderboard
    published = sorted(PublishedRound.objects.filter(tenant=tenant)
                       .values_list('round_nb', flat=True))
    cache.delete(f'tournament:{tenant.subdomain}')
    invalidate_leaderboard(tenant.subdomain)
    broadcast_publish_state(tenant.subdomain, {'published_rounds': published})
    broadcast_display(tenant.subdomain, 'tournament.update', {'event': 'tournament_update'})
    broadcast_display(tenant.subdomain, 'screen.update', {'event': 'screens_changed'})
