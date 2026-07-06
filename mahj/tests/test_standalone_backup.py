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

    def test_no_marker_is_noop(self, db):
        assert sb.apply_pending_restore() is None

    def test_bad_marker_is_cleared_not_wedged(self, db):
        # A marker pointing at a vanished snapshot must not fail every launch.
        (db.parent / sb._PENDING_MARKER).write_text('ghost.sqlite3')
        assert sb.apply_pending_restore() is None
        assert not (db.parent / sb._PENDING_MARKER).exists()


class TestPublicSiteUrl:
    """The spectator-site URL advertised on screens/cards: PUBLIC_SITE_URL wins,
    else the tenant's <subdomain>.mahj.ovh (cloud default)."""

    def test_fallback_to_subdomain(self, settings):
        from mahj.views.helpers import public_site_url, public_site_host
        settings.PUBLIC_SITE_URL = ''
        assert public_site_url('oemc2026') == 'https://oemc2026.mahj.ovh'
        assert public_site_host('oemc2026') == 'oemc2026.mahj.ovh'

    def test_override_used_when_set(self, settings):
        from mahj.views.helpers import public_site_url, public_site_host
        settings.PUBLIC_SITE_URL = 'https://scores.example.org'
        assert public_site_url('local') == 'https://scores.example.org'
        assert public_site_host('local') == 'scores.example.org'

    def test_override_without_scheme_gets_https(self, settings):
        from mahj.views.helpers import public_site_url
        settings.PUBLIC_SITE_URL = 'scores.example.org'
        assert public_site_url('local') == 'https://scores.example.org'
