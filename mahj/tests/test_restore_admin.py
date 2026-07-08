"""Database-restore admin console: gating, typed-confirmation, dump validation,
and the grouped backup listing.

The destructive worker itself (pause pool / drop / pg_restore) needs a real
Postgres + pgbouncer and is exercised manually (see scripts/DB_RESTORE.md); these
tests cover the request-side guards that keep it safe: staff-only + fresh reauth,
the typed DB-name confirmation, and path/header validation of the chosen dump.
"""
import time

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client

from mahj import restore_queue
from mahj.views import restore_admin
from mahj.views.user_admin import REAUTH_SESSION_KEY

HOST = 'test.example.com'


@pytest.fixture
def client_():
    c = Client()
    c.defaults['HTTP_HOST'] = HOST
    return c


@pytest.fixture
def staff_user(db):
    return User.objects.create_user('staffer', password='pw', is_staff=True)


@pytest.fixture
def plain_user(db):
    return User.objects.create_user('regular', password='pw')


def _reauth(client_):
    """Stamp a fresh password re-confirmation into the session, as user_reauth does."""
    session = client_.session
    session[REAUTH_SESSION_KEY] = time.time()
    session.save()


@pytest.fixture
def no_redis(monkeypatch):
    """Capture enqueued jobs without touching redis_bus."""
    jobs = []
    monkeypatch.setattr(restore_queue, 'enqueue', lambda job: jobs.append(job))
    monkeypatch.setattr(restore_queue, 'new_job_id', lambda: 'testjob')
    return jobs


@pytest.fixture
def backups_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(restore_queue, 'BACKUPS_DIR', tmp_path)
    return tmp_path


def _make_dump(directory, name, header=b'PGDMP', body=b'\x00\x00rest'):
    path = directory / name
    path.write_bytes(header + body)
    return path


# -- gating ------------------------------------------------------------------

class TestGating:
    @pytest.mark.parametrize('path,method', [
        ('/restore_pull', 'post'), ('/restore_run', 'post'), ('/restore_status', 'get'),
    ])
    def test_anonymous_redirected(self, client_, db, path, method):
        resp = getattr(client_, method)(path)
        assert resp.status_code == 302
        assert '/accounts/login/' in resp.url

    @pytest.mark.parametrize('path,method', [
        ('/restore_pull', 'post'), ('/restore_run', 'post'), ('/restore_status', 'get'),
    ])
    def test_staff_without_reauth_blocked(self, client_, staff_user, path, method):
        client_.force_login(staff_user)
        resp = getattr(client_, method)(path)
        assert resp.status_code == 403
        assert resp.json()['status'] == 'reauth_required'

    def test_non_staff_blocked_even_with_reauth(self, client_, plain_user):
        client_.force_login(plain_user)
        _reauth(client_)
        resp = client_.get('/restore_status?job_id=x')
        # staff_only wraps the reauth check, so a non-staff user is redirected.
        assert resp.status_code == 302


# -- restore_run: typed confirmation + dump validation -----------------------

class TestRestoreRun:
    def _login(self, client_, staff_user):
        client_.force_login(staff_user)
        _reauth(client_)

    def test_wrong_confirm_rejected(self, client_, staff_user, backups_dir, no_redis):
        self._login(client_, staff_user)
        _make_dump(backups_dir, 'mahj_cloud_20260630T184242Z.dump')
        resp = client_.post('/restore_run', data={
            'dump': 'mahj_cloud_20260630T184242Z.dump', 'confirm': 'nope',
        }, content_type='application/json')
        assert resp.status_code == 400
        assert no_redis == []          # nothing enqueued

    def test_unknown_dump_rejected(self, client_, staff_user, backups_dir, no_redis):
        self._login(client_, staff_user)
        resp = client_.post('/restore_run', data={
            'dump': 'mahj_cloud_does_not_exist.dump', 'confirm': settings.DATABASES['default']['NAME'],
        }, content_type='application/json')
        assert resp.status_code == 400
        assert no_redis == []

    def test_path_traversal_rejected(self, client_, staff_user, backups_dir, no_redis):
        self._login(client_, staff_user)
        resp = client_.post('/restore_run', data={
            'dump': '../../etc/passwd', 'confirm': settings.DATABASES['default']['NAME'],
        }, content_type='application/json')
        assert resp.status_code == 400
        assert no_redis == []

    def test_missing_header_rejected(self, client_, staff_user, backups_dir, no_redis):
        self._login(client_, staff_user)
        _make_dump(backups_dir, 'mahj_cloud_bad.dump', header=b'NOTPG')
        resp = client_.post('/restore_run', data={
            'dump': 'mahj_cloud_bad.dump', 'confirm': settings.DATABASES['default']['NAME'],
        }, content_type='application/json')
        assert resp.status_code == 400
        assert no_redis == []

    def test_valid_restore_enqueued(self, client_, staff_user, backups_dir, no_redis):
        self._login(client_, staff_user)
        _make_dump(backups_dir, 'mahj_cloud_20260630T184242Z.dump')
        resp = client_.post('/restore_run', data={
            'dump': 'mahj_cloud_20260630T184242Z.dump', 'confirm': settings.DATABASES['default']['NAME'],
        }, content_type='application/json')
        assert resp.status_code == 200
        assert resp.json()['status'] == 'ok'
        assert len(no_redis) == 1
        job = no_redis[0]
        assert job['job_id'] == 'testjob'
        assert job['action'] == 'restore'
        assert job['dump'] == 'mahj_cloud_20260630T184242Z.dump'
        # The admin's session key rides along so the worker can re-insert it after
        # the DB swap (the restore wipes django_session and would log them out).
        assert job['session_key']

    def test_pull_enqueued(self, client_, staff_user, no_redis):
        client_.force_login(staff_user)
        _reauth(client_)
        resp = client_.post('/restore_pull')
        assert resp.status_code == 200
        assert no_redis == [{'job_id': 'testjob', 'action': 'pull'}]


# -- listing + helpers -------------------------------------------------------

class TestListBackups:
    def test_empty_when_no_dir(self, backups_dir):
        # tmp_path exists but holds no dumps
        assert restore_admin.list_backups() == []

    def test_grouped_by_source_venue_first(self, backups_dir):
        for name in ['mahj_cloud_20260630T100000Z.dump',
                     'mahj_cloud_20260630T110000Z.dump',
                     'mahj_venue_20260630T120000Z.dump']:
            _make_dump(backups_dir, name)
        groups = restore_admin.list_backups()
        assert [g['source'] for g in groups] == ['venue', 'cloud']
        cloud = next(g for g in groups if g['source'] == 'cloud')
        assert cloud['count'] == 2
        # Newest first within a source.
        assert cloud['recent'][0]['name'] == 'mahj_cloud_20260630T110000Z.dump'
        assert cloud['newest'] == 'mahj_cloud_20260630T110000Z.dump'

    def test_recent_slice_caps_the_list(self, backups_dir, monkeypatch):
        monkeypatch.setattr(restore_admin, 'RECENT_PER_SOURCE', 2)
        for i in range(5):
            _make_dump(backups_dir, f'mahj_cloud_20260630T10000{i}Z.dump')
        (group,) = restore_admin.list_backups()
        assert group['count'] == 5
        assert len(group['recent']) == 2
        assert group['has_more'] is True


def test_parse_name_handles_underscored_source():
    assert restore_admin._parse_name('mahj_venue_lan_20260630T184242Z.dump')[0] == 'venue_lan'


def test_human_size():
    assert restore_admin._human_size(500) == '500B'
    assert restore_admin._human_size(309000).endswith('K')
