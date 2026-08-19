"""sqlite snapshot / restore-on-relaunch used by the standalone build.

Exercises mahj.standalone_backup directly against a temp sqlite file (no Django
DB needed): snapshots are taken and pruned, integrity is checked, a scheduled
restore is validated and then applied by swapping the file — the whole point
being that the swap happens out-of-process (at launch), which these tests stand
in for by calling apply_pending_restore() directly.
"""
import sqlite3

import pytest

from mahj import standalone_backup as sb


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A live sqlite DB at MAHJ_DB_PATH holding one value we can mutate/track."""
    path = tmp_path / 'mahj.sqlite3'
    monkeypatch.setenv('MAHJ_DB_PATH', str(path))
    con = sqlite3.connect(str(path))
    con.execute('CREATE TABLE t (v TEXT)')
    con.execute("INSERT INTO t VALUES ('original')")
    con.commit()
    con.close()
    return path


def _value(path):
    con = sqlite3.connect(str(path))
    try:
        return con.execute('SELECT v FROM t').fetchone()[0]
    finally:
        con.close()


def _set_value(path, v):
    con = sqlite3.connect(str(path))
    con.execute('UPDATE t SET v = ?', (v,))
    con.commit()
    con.close()


class TestSnapshots:
    def test_take_snapshot_creates_file(self, db):
        snap = sb.take_snapshot()
        assert snap is not None and snap.exists()
        assert _value(snap) == 'original'

    def test_prunes_to_keep_limit(self, db, monkeypatch):
        monkeypatch.setattr(sb, 'SNAPSHOT_KEEP', 3)
        # Microsecond-precision names stay unique even back-to-back.
        for _ in range(5):
            sb.take_snapshot()
        assert len(list(sb.snapshots_dir().glob('mahj-*.sqlite3'))) == 3

    def test_list_groups_shape(self, db):
        sb.take_snapshot()
        groups = sb.list_snapshot_groups()
        assert len(groups) == 1
        g = groups[0]
        assert g['count'] == 1 and g['recent'][0]['name'].endswith('.sqlite3')
        assert 'size_h' in g['recent'][0] and 'ago' in g['recent'][0]


class TestIntegrity:
    def test_good_db_ok(self, db):
        assert sb.integrity_ok(db) is True

    def test_absent_is_ok(self, tmp_path):
        assert sb.integrity_ok(tmp_path / 'nope.sqlite3') is True

    def test_garbage_is_not_ok(self, tmp_path):
        bad = tmp_path / 'bad.sqlite3'
        bad.write_bytes(b'this is not a database' * 100)
        assert sb.integrity_ok(bad) is False


class TestRestore:
    def test_request_rejects_traversal_and_missing(self, db):
        assert sb.request_restore('') is False
        assert sb.request_restore('../etc/passwd') is False
        assert sb.request_restore('does-not-exist.sqlite3') is False

    def test_schedule_then_apply_swaps_the_db(self, db):
        snap = sb.take_snapshot()            # snapshot with 'original'
        _set_value(db, 'changed')            # mutate the live DB
        assert _value(db) == 'changed'

        assert sb.request_restore(snap.name) is True
        applied = sb.apply_pending_restore()
        assert applied == snap.name
        assert _value(db) == 'original'      # rolled back

    def test_apply_backs_up_current_before_swapping(self, db):
        snap = sb.take_snapshot()
        _set_value(db, 'changed')
        before = set(sb.snapshots_dir().glob('mahj-*.sqlite3'))
        sb.request_restore(snap.name)
        sb.apply_pending_restore()
        after = set(sb.snapshots_dir().glob('mahj-*.sqlite3'))
        # A safety snapshot of the 'changed' state was taken during apply.
        assert len(after) > len(before)

    def test_apply_leaves_no_temp_file(self, db):
        # The swap is atomic (copy to a temp then os.replace); the temp must not
        # linger next to the live DB.
        snap = sb.take_snapshot()
        sb.request_restore(snap.name)
        sb.apply_pending_restore()
        assert not (db.parent / (db.name + '.restore-tmp')).exists()

    def test_no_marker_is_noop(self, db):
        assert sb.apply_pending_restore() is None

    def test_bad_marker_is_cleared_not_wedged(self, db):
        # A marker pointing at a vanished snapshot must not fail every launch.
        (db.parent / sb._PENDING_MARKER).write_text('ghost.sqlite3')
        assert sb.apply_pending_restore() is None
        assert not (db.parent / sb._PENDING_MARKER).exists()


class TestPublicSiteUrl:
    """The spectator-site URL advertised on screens/cards: the tenant's
    configured public_url wins, else its <subdomain>.example.com (default)."""

    def test_fallback_to_subdomain(self):
        from mahj.views.helpers import public_site_url, public_site_host
        assert public_site_url('oemc2026') == 'https://oemc2026.example.com'
        assert public_site_host('oemc2026') == 'oemc2026.example.com'

    def test_override_used_when_set(self):
        from mahj.views.helpers import public_site_url, public_site_host
        assert public_site_url('local', 'https://scores.example.org') == 'https://scores.example.org'
        assert public_site_host('local', 'https://scores.example.org') == 'scores.example.org'

    def test_override_without_scheme_gets_https(self):
        from mahj.views.helpers import public_site_url
        assert public_site_url('local', 'scores.example.org') == 'https://scores.example.org'


class TestFailedSnapshotLeavesNothing:
    """A snapshot that fails must not leave a file the restore page will offer.

    sqlite3.connect() creates its destination up front, so writing the backup
    straight to the final name left a 0-byte file behind on failure — and an empty
    file is a *valid empty database*: PRAGMA quick_check says 'ok', so it listed
    and restored as a healthy snapshot, wiping the tournament.
    """

    def test_an_empty_file_really_does_pass_the_integrity_check(self, tmp_path):
        """The premise, pinned: this is why a leftover file is dangerous rather
        than merely untidy."""
        empty = tmp_path / 'empty.sqlite3'
        empty.touch()
        assert empty.stat().st_size == 0
        assert sb.integrity_ok(empty) is True

    def test_failed_backup_leaves_no_listable_snapshot(self, db, monkeypatch):
        real_connect = sqlite3.connect

        class _BackupFails:
            """The source connection, with backup() failing — sqlite3.Connection
            attributes are read-only, so it has to be wrapped rather than patched.
            Stands in for a full disk: connect() has already created the
            destination file, but the backup never completes."""

            def __init__(self, con):
                self._con = con

            def backup(self, *a, **kw):
                raise sqlite3.OperationalError('disk I/O error')

            def close(self):
                self._con.close()

            def __getattr__(self, name):
                return getattr(self._con, name)

        def explode(target, *a, **kw):
            con = real_connect(target, *a, **kw)
            # src.backup(dst) is called on the *source* connection.
            return _BackupFails(con) if str(target) == str(db) else con

        monkeypatch.setattr(sb.sqlite3, 'connect', explode)
        with pytest.raises(sqlite3.OperationalError):
            sb.take_snapshot()

        assert list(sb.snapshots_dir().glob('mahj-*.sqlite3')) == []
        assert list(sb.snapshots_dir().glob('*.tmp')) == []
        assert sb.list_snapshot_groups() == []

    def test_a_successful_snapshot_still_lands_under_its_final_name(self, db):
        snap = sb.take_snapshot()
        assert snap is not None
        assert snap.suffix == '.sqlite3'
        assert snap.exists() and snap.stat().st_size > 0
        assert list(sb.snapshots_dir().glob('*.tmp')) == []
        assert _value(snap) == 'original'


class TestRestoreDoesNotDestroyTheLiveDb:
    """The restore copies the snapshot aside before it touches the live DB's WAL
    sidecars. Dropping them first destroyed un-checkpointed commits before the copy
    that was meant to replace them — and a whole-DB copy failing on a full disk is
    exactly when they are all the operator has left."""

    def _sidecars(self, db):
        wal = sb.Path(str(db) + '-wal')
        shm = sb.Path(str(db) + '-shm')
        wal.write_bytes(b'wal-content')
        shm.write_bytes(b'shm-content')
        return wal, shm

    def test_copy_failure_leaves_the_live_db_and_its_wal_intact(self, db, monkeypatch):
        snap = sb.take_snapshot()
        _set_value(db, 'live')
        assert sb.request_restore(snap.name) is True
        # The safety snapshot opens and closes the live DB, and sqlite checkpoints
        # away the sidecars on a clean close — which would remove the synthetic ones
        # below before the code under test ever reaches them. Stub it out so this
        # test is about the copy/unlink ordering and nothing else.
        monkeypatch.setattr(sb, 'take_snapshot', lambda *a, **kw: None)
        wal, shm = self._sidecars(db)

        def boom(src, dst, *a, **kw):
            raise OSError(28, 'No space left on device')

        monkeypatch.setattr(sb.shutil, 'copyfile', boom)
        with pytest.raises(OSError):
            sb.apply_pending_restore()

        # Nothing about the live database was touched. Check the sidecars *first*:
        # reading the DB opens and closes it, and sqlite checkpoints them away on a
        # clean close, which would destroy the evidence.
        assert wal.exists() and wal.read_bytes() == b'wal-content'
        assert shm.exists() and shm.read_bytes() == b'shm-content'
        assert not sb.Path(str(db) + '.restore-tmp').exists()
        assert _value(db) == 'live'

    def test_successful_restore_still_drops_the_sidecars_and_swaps(self, db):
        snap = sb.take_snapshot()          # holds 'original'
        _set_value(db, 'live')
        wal, shm = self._sidecars(db)
        assert sb.request_restore(snap.name) is True

        assert sb.apply_pending_restore() == snap.name
        assert _value(db) == 'original'
        # The sidecars must be gone — they would otherwise replay onto the
        # restored file and undo it.
        assert not wal.exists()
        assert not shm.exists()
        assert not sb.Path(str(db) + '.restore-tmp').exists()
