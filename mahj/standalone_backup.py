"""sqlite backup/restore for the standalone build.

The launcher (standalone/run.py) takes rolling online-backup snapshots and, on
startup, applies any restore the admin scheduled. The admin "Snapshot restore"
page (mahj/views/restore_admin.py) lists these snapshots and schedules a restore
by writing a marker file; it takes effect on the next launch, because a single
process can't safely swap the sqlite file it currently holds open (WAL).

This rolls back the whole local database. To move or restore a single tournament
(here or on a cloud install), see mahj/tenant_dump.py. Paths derive from
MAHJ_DB_PATH (set by the launcher), so the module needs no Django settings and is
safe to import before django.setup() — the launcher calls apply_pending_restore()
before Django opens the database.
"""
import datetime
import os
import shutil
import sqlite3
from pathlib import Path

SNAPSHOT_KEEP = 12          # ~1 hour at the launcher's 5-minute cadence
RECENT_SHOWN = 20           # newest N surfaced on the restore page
_PENDING_MARKER = 'restore_pending'
# The token the operator types to confirm a restore (mirrors the pg DB name on
# the Postgres path). Not a real filename check — just a deliberate-action gate.
CONFIRM_TOKEN = 'mahj.sqlite3'


def db_path():
    return Path(os.environ['MAHJ_DB_PATH'])


def _data_dir():
    return db_path().parent


def snapshots_dir():
    d = _data_dir() / 'snapshots'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pending_path():
    return _data_dir() / _PENDING_MARKER


def integrity_ok(path):
    """PRAGMA quick_check a sqlite file. True if healthy or absent (nothing yet)."""
    p = Path(path)
    if not p.exists():
        return True
    try:
        con = sqlite3.connect(str(p))
        try:
            row = con.execute('PRAGMA quick_check;').fetchone()
        finally:
            con.close()
        return bool(row) and row[0] == 'ok'
    except sqlite3.DatabaseError:
        return False


def sweep_orphan_staging():
    """Delete leftover *.tmp staging files in snapshots/.

    take_snapshot removes its own on failure, so one survives only a kill or a
    power cut mid-backup — and each is a full copy of the database, so a few of
    them fill a laptop's disk. Nothing else matches them (they are outside the
    'mahj-*.sqlite3' glob), so nothing else would ever clean them up. Called at
    boot, where no backup can be in flight.
    """
    for stale in snapshots_dir().glob('*.tmp'):
        stale.unlink(missing_ok=True)


def take_snapshot(prune=True):
    """Online-backup the live DB into snapshots/, optionally pruning to
    SNAPSHOT_KEEP.

    Uses sqlite's backup API, which is safe against a concurrently-written DB —
    unlike copying the file. The filename carries microseconds so two snapshots in
    the same second (e.g. the boot snapshot and the safety snapshot taken during a
    restore) never collide. `prune=False` skips pruning — used by the restore path
    so it can't delete the snapshot it's about to restore from. Returns the
    snapshot Path, or None if there's no DB yet.
    """
    src_path = db_path()
    if not src_path.exists():
        return None
    dest = snapshots_dir() / f"mahj-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.sqlite3"
    # Build the snapshot under a .tmp name and rename it into place only once the
    # backup has finished. sqlite3.connect() creates the destination file up front,
    # so writing straight to `dest` leaves a 0-byte file behind when backup() fails
    # (a full disk, a corrupt source) — and a 0-byte file is a *valid empty*
    # database: PRAGMA quick_check returns 'ok', so it would be offered on the
    # restore page as a healthy snapshot and silently wipe the tournament. The .tmp
    # suffix is also outside the 'mahj-*.sqlite3' glob that lists and prunes
    # snapshots, so a leftover can never be listed either.
    staging = dest.with_suffix('.tmp')
    try:
        src = sqlite3.connect(str(src_path))
        dst = sqlite3.connect(str(staging))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        os.replace(staging, dest)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    if prune:
        snaps = sorted(snapshots_dir().glob('mahj-*.sqlite3'))
        for old in snaps[:-SNAPSHOT_KEEP]:
            old.unlink(missing_ok=True)
    return dest


def request_restore(name):
    """Schedule snapshot `name` to be restored on the next launch.

    Returns True if `name` is a healthy snapshot in snapshots/ (no path
    traversal); False otherwise. The swap itself happens in
    apply_pending_restore() at startup.
    """
    if not name or name != os.path.basename(name):
        return False
    snap = snapshots_dir() / name
    if not snap.is_file() or not integrity_ok(snap):
        return False
    _pending_path().write_text(name, encoding='utf-8')
    return True


def apply_pending_restore():
    """If a restore was scheduled, swap it in before Django opens the DB.

    Backs up the current DB first (so a mistaken restore is itself recoverable),
    drops the WAL sidecars (they must not replay onto the restored file), then
    copies the snapshot over the live DB. Returns the applied name, or None.
    Always clears the marker, even on a bad/absent snapshot, so a broken marker
    can't wedge every launch.
    """
    marker = _pending_path()
    if not marker.exists():
        return None
    name = marker.read_text(encoding='utf-8').strip()
    marker.unlink(missing_ok=True)

    snap = snapshots_dir() / name
    if not (name and os.path.basename(name) == name and snap.is_file() and integrity_ok(snap)):
        return None

    dbp = db_path()
    if dbp.exists():
        # Best-effort safety copy of the current DB so a mistaken restore is itself
        # recoverable. Never let it block the restore: the operator may be
        # restoring *because* the live DB is corrupt, and snapshotting a corrupt
        # DB can fail — the restore source is already integrity-checked above.
        try:
            take_snapshot(prune=False)  # never prune the restore source
        except Exception:
            pass
    # Order matters. Copy the snapshot aside first, and only once that has
    # succeeded drop the live DB's WAL sidecars and swap. Dropping them first would
    # destroy the live DB's un-checkpointed commits before the copy that replaces
    # it — and a whole-DB copy failing on a full disk is exactly the case where
    # they are all the operator has left, since the safety snapshot above is
    # best-effort. os.replace is an atomic rename within the filesystem, so a crash
    # mid-write can't leave a truncated live DB either.
    tmp = Path(str(dbp) + '.restore-tmp')
    try:
        shutil.copyfile(snap, tmp)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    for suffix in ('-wal', '-shm'):
        side = Path(str(dbp) + suffix)
        if side.exists():
            side.unlink()
    os.replace(tmp, dbp)
    return name


# ---- Listing for the admin restore page -------------------------------------

def _human_size(n):
    size = float(n)
    for unit in ('B', 'K', 'M', 'G'):
        if size < 1024 or unit == 'G':
            return f"{size:.0f}{unit}" if unit == 'B' else f"{size:.1f}{unit}"
        size /= 1024


def _ago(mtime):
    secs = datetime.datetime.now().timestamp() - mtime
    if secs < 90:
        return 'just now'
    if secs < 5400:
        return f"{round(secs / 60)}m ago"
    if secs < 172800:
        return f"{round(secs / 3600)}h ago"
    return f"{round(secs / 86400)}d ago"


def list_snapshot_groups():
    """The snapshots the restore page lists: a single 'local' group, newest first."""
    items = []
    for p in snapshots_dir().glob('mahj-*.sqlite3'):
        try:
            st = p.stat()
        except OSError:
            continue
        when = datetime.datetime.fromtimestamp(st.st_mtime)
        items.append({
            'name': p.name,
            'source': 'local',
            'size_h': _human_size(st.st_size),
            'iso': '',  # local-time stamp; let 'when' render as-is
            'when': when.strftime('%d %b %H:%M'),
            'ago': _ago(st.st_mtime),
            'sort': st.st_mtime,
        })
    if not items:
        return []
    items.sort(key=lambda d: d['sort'], reverse=True)
    return [{
        'source': 'local',
        'count': len(items),
        'newest': items[0]['name'],
        'newest_ago': items[0]['ago'],
        'newest_when': items[0]['when'],
        'recent': items[:RECENT_SHOWN],
        'has_more': len(items) > RECENT_SHOWN,
    }]
