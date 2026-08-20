"""Behavioural tests for the reworked scan flow.

- scan_prefill persists per-hand OCR confidence and leaves the sheet NOT valid
  (review/validation now happens on the score sheet, not during scanning).
- The pre-fill scan route (scan_<r>_<t>) renders with round/table filled in.
- The score sheet renders a QR linking back to the pre-filled scan page.
"""
import json
import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from mahj.models import Hand, ScoreSheet
from mahj.tests.conftest import grant


HOST = 'test.example.com'


@pytest.fixture
def client_():
    c = Client()
    c.defaults['HTTP_HOST'] = HOST
    return c


@pytest.fixture
def scorer(tournament):
    u = User.objects.create_user('scorer', password='pw')
    grant(u, tournament['tenant'], scorer=True)
    return u


@pytest.fixture
def finished_job(monkeypatch):
    """Stage an OCR job result the way a real scan leaves one.

    The round, table and tenant live on the *job* — recorded from the scan URL when
    the image was staged — not in the request that later writes it. That is the whole
    of F13: a request body can name any table, a job cannot.
    """
    jobs = {}

    def stage(job_id, round_nb=3, table_nb=1, scores=None,
              subdomain='test', status='done'):
        jobs[job_id] = {
            'status': status, 'round_nb': round_nb, 'table_nb': table_nb,
            'subdomain': subdomain, 'scores': scores if scores is not None else [],
        }
        return job_id

    monkeypatch.setattr('mahj.scan_queue.get_result', lambda jid: jobs.get(jid))
    return stage


def _prefill(client, **body):
    return client.post('/scan_prefill', data=json.dumps(body),
                       content_type='application/json')


class TestScanPrefill:
    def test_persists_confidence_and_leaves_not_valid(self, client_, tournament, scorer,
                                                      finished_job):
        client_.force_login(scorer)
        tenant = tournament['tenant']
        # Round 3 has seats but no hands seeded — an "empty" table, no conflict.
        finished_job('job1', round_nb=3, table_nb=1, scores=[
            {'Hand': 1, 'Value': 20, 'Winner': 1, 'Discarder': 2, 'Confidence': 'unsure'},
            {'Hand': 2, 'Value': 16, 'Winner': 3, 'Discarder': None, 'Confidence': 'certain'},
        ])
        resp = _prefill(client_, job_id='job1')
        assert resp.status_code == 200
        assert resp.json()['ok'] is True

        h1 = Hand.objects.get(tenant=tenant, round_nb=3, table_nb=1, hand_nb=1)
        assert h1.points == 20 and h1.win_by == 1 and h1.win_from == 2
        assert h1.confidence == pytest.approx(0.3)  # 'unsure'

        h2 = Hand.objects.get(tenant=tenant, round_nb=3, table_nb=1, hand_nb=2)
        assert h2.confidence == pytest.approx(1.0)  # 'certain'

        # Sheet stays NOT valid: the ScoreSheet exists but is not validated.
        sheet = ScoreSheet.objects.get(tenant=tenant, round_nb=3, table_nb=1)
        assert sheet.validated is False

    def test_filled_table_conflicts_for_anonymous_no_force(self, client_, tournament,
                                                           finished_job):
        """A filled table is never overwritten by a scan — there is no `force`
        escape, and no login is required to be refused."""
        tenant = tournament['tenant']
        h = Hand.objects.create(
            tenant=tenant, round_nb=3, table_nb=3, hand_nb=1,
            points=20, win_by=1, win_from=2, confidence=1.0,
        )
        finished_job('job2', round_nb=3, table_nb=3, scores=[
            {'Hand': 1, 'Value': 999, 'Winner': 1, 'Discarder': 2, 'Confidence': 1.0}])
        resp = _prefill(client_, job_id='job2', force=True)
        assert resp.status_code == 409
        assert resp.json()['conflict'] is True
        # `force` is ignored; the original row is intact.
        h.refresh_from_db()
        assert h.points == 20


class TestScanPrefillTrustsOnlyTheJob:
    """F13. The endpoint is anonymous by design — a player photographs their own
    sheet — but it used to take the scores, the round and the table from the request
    body, and default `validate` to true. So a caller needed no photo at all: POST
    hand values for any empty table and it was written *and* marked validated,
    skipping scorer review entirely."""

    def test_scores_in_the_body_are_ignored(self, client_, tournament, finished_job):
        tenant = tournament['tenant']
        finished_job('j', round_nb=3, table_nb=1, scores=[
            {'Hand': 1, 'Value': 20, 'Winner': 1, 'Discarder': 2, 'Confidence': 'certain'}])
        # The body tries to smuggle a different value in alongside the job.
        resp = _prefill(client_, job_id='j', scores=[
            {'Hand': 1, 'Value': 999, 'Winner': 4, 'Discarder': 1, 'Confidence': 'certain'}])
        assert resp.status_code == 200
        h = Hand.objects.get(tenant=tenant, round_nb=3, table_nb=1, hand_nb=1)
        assert h.points == 20 and h.win_by == 1   # the job's reading, not the body's

    def test_the_table_comes_from_the_job(self, client_, tournament, finished_job):
        """A body naming another table must not redirect the write."""
        tenant = tournament['tenant']
        finished_job('j', round_nb=3, table_nb=1, scores=[
            {'Hand': 1, 'Value': 20, 'Winner': 1, 'Discarder': 2, 'Confidence': 'certain'}])
        resp = _prefill(client_, job_id='j', round_nb=3, table_nb=4)
        assert resp.status_code == 200
        assert Hand.objects.filter(tenant=tenant, round_nb=3, table_nb=1,
                                   win_by__isnull=False).exists()
        assert not Hand.objects.filter(tenant=tenant, round_nb=3, table_nb=4,
                                       win_by__isnull=False).exists()

    def test_anonymous_cannot_validate(self, client_, tournament, finished_job):
        tenant = tournament['tenant']
        finished_job('j', round_nb=3, table_nb=1, scores=[
            {'Hand': 1, 'Value': 20, 'Winner': 1, 'Discarder': 2, 'Confidence': 'certain'}])
        resp = _prefill(client_, job_id='j', validate=True)
        assert resp.status_code == 200
        sheet = ScoreSheet.objects.get(tenant=tenant, round_nb=3, table_nb=1)
        assert sheet.validated is False

    def test_a_scorer_can_validate(self, client_, tournament, scorer, finished_job):
        client_.force_login(scorer)
        tenant = tournament['tenant']
        finished_job('j', round_nb=3, table_nb=1, scores=[
            {'Hand': 1, 'Value': 20, 'Winner': 1, 'Discarder': 2, 'Confidence': 'certain'}])
        resp = _prefill(client_, job_id='j', validate=True)
        assert resp.status_code == 200
        assert ScoreSheet.objects.get(
            tenant=tenant, round_nb=3, table_nb=1).validated is True

    def test_validation_is_off_unless_asked_for(self, client_, tournament, scorer,
                                                finished_job):
        """It used to default to on, which is how an anonymous POST validated."""
        client_.force_login(scorer)
        finished_job('j', round_nb=3, table_nb=1, scores=[
            {'Hand': 1, 'Value': 20, 'Winner': 1, 'Discarder': 2, 'Confidence': 'certain'}])
        _prefill(client_, job_id='j')
        assert ScoreSheet.objects.get(
            tenant=tournament['tenant'], round_nb=3, table_nb=1).validated is False

    def test_no_job_id_is_400(self, client_, tournament):
        assert _prefill(client_).status_code == 400

    def test_an_unknown_job_is_404(self, client_, tournament, finished_job):
        resp = _prefill(client_, job_id='never-existed')
        assert resp.status_code == 404
        assert resp.json()['status'] == 'expired'

    def test_a_job_still_reading_is_409(self, client_, tournament, finished_job):
        finished_job('j', status='pending')
        assert _prefill(client_, job_id='j').status_code == 409

    def test_another_tenants_job_is_404(self, client_, tournament, finished_job):
        finished_job('j', subdomain='somewhere-else')
        assert _prefill(client_, job_id='j').status_code == 404

    def test_a_job_with_no_table_is_refused(self, client_, tournament, finished_job):
        """Staged from the bare /scan URL, so the server never recorded a target."""
        finished_job('j', round_nb=None, table_nb=None)
        resp = _prefill(client_, job_id='j')
        assert resp.status_code == 400
        assert 'round and table' in resp.json()['error']

    def test_an_unplaceable_row_is_skipped_not_a_500(self, client_, tournament,
                                                     finished_job):
        tenant = tournament['tenant']
        finished_job('j', round_nb=3, table_nb=1, scores=[
            {'Hand': 'nonsense', 'Value': 20, 'Winner': 1},
            'not even a dict',
            {'Value': 20, 'Winner': 1},                       # no Hand at all
            {'Hand': 2, 'Value': 20, 'Winner': 1, 'Discarder': 2, 'Confidence': 'certain'},
        ])
        resp = _prefill(client_, job_id='j')
        assert resp.status_code == 200
        # The readable row still landed.
        assert Hand.objects.get(tenant=tenant, round_nb=3, table_nb=1,
                                hand_nb=2).points == 20


class TestScanConfidence:
    def test_manual_edit_resets_confidence(self, client_, tournament, scorer):
        client_.force_login(scorer)
        tenant = tournament['tenant']
        h = Hand.objects.create(
            tenant=tenant, round_nb=3, table_nb=2, hand_nb=1,
            points=20, win_by=1, win_from=2, confidence=0.3,
        )
        resp = client_.post('/update_hand_points', {
            'id': h.id, 'version': h.version, 'points': 24, 'by': 1, 'from': 2,
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
        # A scorer can open the score sheet inline.
        assert "canOpenSheet = true" in html

    def test_anonymous_page_cannot_open_sheet(self, client_, tournament):
        html = client_.get('/scan').content.decode()
        # Not signed in as a scorer → pointed to the admin console instead.
        assert "canOpenSheet = false" in html

    def test_scan_seats_does_not_leak_scores_to_anonymous(self, client_, tournament):
        """scan_seats is anonymous, so it must return seat labels + filled/valid
        flags only — never minipoints/tablepoints, which are withheld from the
        public until a round is published."""
        # Round 1 table 1 is complete and validated in the fixture (has scores).
        resp = client_.get('/scan_seats?round_nb=1&table_nb=1')
        assert resp.status_code == 200
        data = resp.json()
        assert data['ok'] and data['seats']
        assert data['has_hands'] and data['validated']
        for seat in data['seats']:
            assert 'mp' not in seat
            assert 'tp' not in seat
            assert seat['player']  # the label is still there


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
