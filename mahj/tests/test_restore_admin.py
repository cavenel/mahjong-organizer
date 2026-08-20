"""Snapshot-restore endpoint (standalone build): gating, the typed confirmation,
and the fact that a cloud install doesn't offer it at all.

The swap itself (drop the WAL sidecars, copy the snapshot over the live DB)
happens at launch and is covered in test_standalone_backup.py. Per-tenant dump
restore — the cloud path — is test_tenant_dump.py.
"""
import time

import pytest
from django.contrib.auth.models import User
from django.test import Client, override_settings

from mahj.views.user_admin import REAUTH_SESSION_KEY
from mahj.tests.conftest import grant

HOST = 'test.example.com'
CONFIRM = 'mahj.sqlite3'   # standalone_backup.CONFIRM_TOKEN


@pytest.fixture
def client_():
    c = Client()
    c.defaults['HTTP_HOST'] = HOST
    return c


@pytest.fixture
def super_user(db):
    # A snapshot restore replaces the whole database, so it stays superuser-gated.
    return User.objects.create_superuser('operator', password='pw')


@pytest.fixture
def staff_user(tenant):
    # A per-tenant admin (NOT a platform superuser), who must not reach it.
    u = User.objects.create_user('staffer', password='pw')
    grant(u, tenant, admin=True)
    return u


def _reauth(client_):
    """Stamp a fresh password re-confirmation into the session, as user_reauth does."""
    session = client_.session
    session[REAUTH_SESSION_KEY] = time.time()
    session.save()


@pytest.fixture
def snapshots(tmp_path, monkeypatch):
    """A standalone data dir with one healthy snapshot in it."""
    import sqlite3
    monkeypatch.setenv('MAHJ_DB_PATH', str(tmp_path / 'mahj.sqlite3'))
    snaps = tmp_path / 'snapshots'
    snaps.mkdir()
    snap = snaps / 'mahj-20260820-120000-000000.sqlite3'
    sqlite3.connect(str(snap)).close()   # a valid (empty) sqlite file
    return snap


class TestGating:
    def test_anonymous_redirected(self, client_, db):
        resp = client_.post('/restore_run')
        assert resp.status_code == 302
        assert '/accounts/login/' in resp.url

    def test_superuser_without_reauth_blocked(self, client_, super_user):
        client_.force_login(super_user)
        resp = client_.post('/restore_run')
        assert resp.status_code == 403
        assert resp.json()['status'] == 'reauth_required'

    def test_tenant_admin_blocked_even_with_reauth(self, client_, staff_user):
        """A per-tenant admin restores their own tournament from a dump; rolling the
        whole database back stays a platform-operator action."""
        client_.force_login(staff_user)
        _reauth(client_)
        assert client_.post('/restore_run').status_code == 403


class TestRestoreRun:
    def _login(self, client_, super_user):
        client_.force_login(super_user)
        _reauth(client_)

    def test_unavailable_outside_the_standalone_build(self, client_, super_user, snapshots):
        """A cloud install has no snapshots — it restores a tournament dump instead."""
        self._login(client_, super_user)
        resp = client_.post('/restore_run',
                            data={'dump': snapshots.name, 'confirm': CONFIRM},
                            content_type='application/json')
        assert resp.status_code == 404

    @override_settings(STANDALONE=True)
    def test_wrong_confirm_rejected(self, client_, super_user, snapshots):
        self._login(client_, super_user)
        resp = client_.post('/restore_run',
                            data={'dump': snapshots.name, 'confirm': 'nope'},
                            content_type='application/json')
        assert resp.status_code == 400
        assert not (snapshots.parent.parent / 'restore_pending').exists()

    @override_settings(STANDALONE=True)
    def test_unknown_snapshot_rejected(self, client_, super_user, snapshots):
        self._login(client_, super_user)
        resp = client_.post('/restore_run',
                            data={'dump': 'mahj-nope.sqlite3', 'confirm': CONFIRM},
                            content_type='application/json')
        assert resp.status_code == 400

    @override_settings(STANDALONE=True)
    def test_path_traversal_rejected(self, client_, super_user, snapshots):
        self._login(client_, super_user)
        resp = client_.post('/restore_run',
                            data={'dump': '../../etc/passwd', 'confirm': CONFIRM},
                            content_type='application/json')
        assert resp.status_code == 400

    @override_settings(STANDALONE=True)
    def test_valid_restore_is_scheduled_for_the_next_launch(self, client_, super_user, snapshots):
        self._login(client_, super_user)
        resp = client_.post('/restore_run',
                            data={'dump': snapshots.name, 'confirm': CONFIRM},
                            content_type='application/json')
        assert resp.status_code == 200
        body = resp.json()
        assert body['status'] == 'ok' and body['standalone'] is True
        # The marker the launcher reads before Django opens the DB.
        marker = snapshots.parent.parent / 'restore_pending'
        assert marker.read_text() == snapshots.name


@override_settings(STANDALONE=True)
def test_confirm_dialog_renders_seat_counts(client_, super_user, tournament, snapshots):
    """The "what you're about to overwrite" counts come from `db_counts`, whose
    Seat tally is keyed `seats` — a mismatch would silently render blanks."""
    import re
    client_.force_login(super_user)
    _reauth(client_)
    body = client_.get('/admin?page=database_restore').content.decode()
    m = re.search(r'<strong>(\d+)</strong> players, <strong>(\d+)</strong> seats,\s*'
                  r'<strong>(\d+)</strong> hands', body)
    assert m, [l.strip() for l in body.splitlines() if 'players,' in l][:5]
    assert (int(m.group(1)), int(m.group(2))) == (16, 48)


def test_page_is_hidden_on_a_cloud_install(client_, super_user, tournament):
    """Not STANDALONE: the page renders as the shell's blank panel, the same as any
    page this account may not see."""
    client_.force_login(super_user)
    _reauth(client_)
    resp = client_.get('/admin?page=database_restore')
    assert resp.context['page_content'] == 'None'
