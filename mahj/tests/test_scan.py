"""Behavioural tests for the reworked scan flow.

- scan_prefill persists per-hand OCR confidence and leaves the sheet NOT valid
  (review/validation now happens on the score sheet, not during scanning).
- The pre-fill scan route (scan_<r>_<t>) renders with round/table filled in.
- The score sheet renders a QR linking back to the pre-filled scan page.
"""
import json
import pytest
from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from mahj.models import Hand


HOST = 'test.mahj.ovh'


@pytest.fixture
def client_():
    c = Client()
    c.defaults['HTTP_HOST'] = HOST
    return c


@pytest.fixture
def scorer(db):
    u = User.objects.create_user('scorer', password='pw')
    group, _ = Group.objects.get_or_create(name='Scorer')
    u.groups.add(group)
    return u


class TestScanPrefill:
    def test_persists_confidence_and_leaves_not_valid(self, client_, tournament, scorer):
        client_.force_login(scorer)
        tenant = tournament['tenant']
        # Round 3 has positions but no hands seeded — an "empty" table, no conflict.
        body = {
            'round_nb': 3, 'table_nb': 1, 'validate': False,
            'scores': [
                {'Hand': 1, 'Value': 20, 'Winner': 1, 'Discarder': 2, 'Confidence': 0.4},
                {'Hand': 2, 'Value': 16, 'Winner': 3, 'Discarder': None, 'Confidence': 0.95},
            ],
        }
        resp = client_.post('/scan_prefill', data=json.dumps(body), content_type='application/json')
        assert resp.status_code == 200
        assert resp.json()['ok'] is True

        h1 = Hand.objects.get(tenant=tenant, round_nb=3, table_nb=1, hand_nb=1)
        assert h1.pts == 20 and h1.win_by == 1 and h1.win_from == 2
        assert h1.confidence == pytest.approx(0.4)

        h2 = Hand.objects.get(tenant=tenant, round_nb=3, table_nb=1, hand_nb=2)
        assert h2.confidence == pytest.approx(0.95)

        # Sheet stays NOT valid: the validation marker (hand_nb=17) is pts=0.
        valid = Hand.objects.get(tenant=tenant, round_nb=3, table_nb=1, hand_nb=17)
        assert valid.pts == 0

    def test_manual_edit_resets_confidence(self, client_, tournament, scorer):
        client_.force_login(scorer)
        tenant = tournament['tenant']
        h = Hand.objects.create(
            tenant=tenant, round_nb=3, table_nb=2, hand_nb=1,
            pts=20, win_by=1, win_from=2, confidence=0.3,
        )
        resp = client_.post('/update_hand_points', {
            'id': h.id, 'version': h.version, 'pts': 24, 'by': 1, 'from': 2,
        })
        assert resp.status_code == 200
        h.refresh_from_db()
        assert h.confidence == pytest.approx(1.0)


class TestScanPrefillPage:
    def test_prefill_route_renders_with_values(self, client_, tournament, scorer):
        client_.force_login(scorer)
        resp = client_.get('/scan_2_3')
        assert resp.status_code == 200
        html = resp.content.decode()
        # round_nb / table_nb reach the template context for client-side pre-fill.
        assert "ctxRound = '2'" in html
        assert "ctxTable = '3'" in html


class TestScanEnqueue:
    """POST /scan stages the image + enqueues a job and returns instantly;
    the heavy OCR runs in the scan_worker, not on the request worker."""

    def test_scan_enqueues_and_returns_job_id(self, client_, tournament, scorer, monkeypatch):
        client_.force_login(scorer)
        captured = {}

        def fake_stage(raw):
            captured['bytes'] = raw
            return 'job-abc'

        def fake_enqueue(job):
            captured['job'] = job

        monkeypatch.setattr('mahj.scan_queue.stage_image', fake_stage)
        monkeypatch.setattr('mahj.scan_queue.enqueue', fake_enqueue)

        img = SimpleUploadedFile('sheet.jpg', b'\xff\xd8\xffnot-a-real-jpeg', content_type='image/jpeg')
        resp = client_.post('/scan_2_3', {'image': img})

        assert resp.status_code == 200
        body = resp.json()
        assert body['ok'] is True and body['job_id'] == 'job-abc'
        # The view does no OCR itself — it only hands the bytes to the queue.
        assert captured['bytes'] == b'\xff\xd8\xffnot-a-real-jpeg'
        assert captured['job']['job_id'] == 'job-abc'
        assert captured['job']['round_nb'] == 2 and captured['job']['table_nb'] == 3

    def test_scan_without_image_is_400(self, client_, tournament, scorer):
        client_.force_login(scorer)
        resp = client_.post('/scan', {})
        assert resp.status_code == 400
        assert resp.json()['ok'] is False


class TestScanStatus:
    def test_status_requires_job_id(self, client_, scorer):
        client_.force_login(scorer)
        assert client_.get('/scan_status').status_code == 400

    def test_status_reports_pending_done_and_expired(self, client_, scorer, monkeypatch):
        client_.force_login(scorer)
        results = {
            'p': {'status': 'pending'},
            'd': {'status': 'done', 'scores': [{'Hand': 1, 'Value': 20}]},
            'e': {'status': 'error', 'error': 'bad photo'},
            'x': None,
        }
        monkeypatch.setattr('mahj.scan_queue.get_result', lambda jid: results[jid])

        assert client_.get('/scan_status?job_id=p').json()['status'] == 'pending'
        done = client_.get('/scan_status?job_id=d').json()
        assert done['status'] == 'done' and done['scores'][0]['Value'] == 20
        err = client_.get('/scan_status?job_id=e').json()
        assert err['ok'] is False and err['error'] == 'bad photo'
        expired = client_.get('/scan_status?job_id=x').json()
        assert expired['ok'] is False and expired['status'] == 'expired'


class TestScoreSheetQr:
    def test_score_sheet_renders_qr(self, client_, tournament, scorer):
        client_.force_login(scorer)
        resp = client_.get('/scores_per_hand_1_1')
        assert resp.status_code == 200
        html = resp.content.decode()
        assert '<svg' in html  # QR code rendered inline
