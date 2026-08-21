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
from django.test import Client, override_settings

from mahj.models import Hand, ScoreSheet
from mahj.tests.conftest import grant


HOST = 'test.example.com'

# A real 2x2 PNG. The upload path decodes the bytes to reject junk before it stages
# the file and buys a vision call, so a placeholder like b'x' * 100 is refused — and
# _looks_like_an_image fails *open* on ImportError, so tests posting junk passed only
# where the imaging stack was absent, and failed in CI and in the built image.
REAL_IMAGE = bytes.fromhex(
    '89504e470d0a1a0a0000000d4948445200000002000000020802000000fdd49a73'
    '0000000e49444154789c63a807030608050029ba05f517f4e93a0000000049454e44ae426082')


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


LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                      'LOCATION': 'scan-limiter'}}


class TestUploadLimiterKeysOnTheRealPeer:
    """The limiter's bucket key must not be under the caller's control.

    nginx appended the real peer to whatever the client sent, so the header read
    "<attacker value>, <real ip>" and the limiter took the first element. Rotating it
    bought a fresh 6-per-minute allowance every request — on an endpoint that is
    anonymous by design and queues a paid vision call per upload.
    """

    @pytest.fixture(autouse=True)
    def _locmem(self):
        # The suite runs on DummyCache, which cannot count.
        from django.core.cache import cache
        with override_settings(CACHES=LOCMEM):
            cache.clear()
            yield
            cache.clear()

    def _allowed(self, spoof, peer='10.0.0.7'):
        from django.test import RequestFactory
        from mahj.views.scan import _upload_allowed
        req = RequestFactory().post('/scan_3_1')
        # Exactly what nginx used to produce: client value first, real peer appended.
        req.META['HTTP_X_FORWARDED_FOR'] = f'{spoof}, {peer}'
        req.META['REMOTE_ADDR'] = peer
        return _upload_allowed(req)

    def test_a_rotating_header_cannot_buy_a_fresh_allowance(self):
        from mahj.views.scan import UPLOAD_MAX_PER_WINDOW
        # A different spoofed value every time, same real peer.
        results = [self._allowed(f'203.0.113.{i}') for i in range(UPLOAD_MAX_PER_WINDOW + 2)]
        assert results[:UPLOAD_MAX_PER_WINDOW] == [True] * UPLOAD_MAX_PER_WINDOW
        assert results[UPLOAD_MAX_PER_WINDOW:] == [False, False], (
            'rotating the client half of X-Forwarded-For must not reset the bucket')

    def test_two_real_peers_get_their_own_buckets(self):
        """The limiter must still be per-device, not global."""
        from mahj.views.scan import UPLOAD_MAX_PER_WINDOW
        for _ in range(UPLOAD_MAX_PER_WINDOW + 1):
            self._allowed('203.0.113.9', peer='10.0.0.7')
        assert self._allowed('203.0.113.9', peer='10.0.0.7') is False
        # A different phone is unaffected.
        assert self._allowed('203.0.113.9', peer='10.0.0.8') is True

    def test_no_forwarded_header_falls_back_to_remote_addr(self):
        from django.test import RequestFactory
        from mahj.views.scan import UPLOAD_MAX_PER_WINDOW, _upload_allowed
        def req(peer):
            r = RequestFactory().post('/scan_3_1')
            r.META['REMOTE_ADDR'] = peer
            return r
        for _ in range(UPLOAD_MAX_PER_WINDOW):
            assert _upload_allowed(req('192.0.2.5')) is True
        assert _upload_allowed(req('192.0.2.5')) is False
        assert _upload_allowed(req('192.0.2.6')) is True


class TestScanPrefillGuardsTheChart:
    """A scan writes 16 Hands and a ScoreSheet, so it needs the same seating-chart
    guard the score sheet itself carries — and it must never undo a validation."""

    def test_a_table_outside_the_chart_writes_nothing(self, client_, tournament,
                                                      scorer, finished_job):
        client_.force_login(scorer)
        tenant = tournament['tenant']
        finished_job('job_ghost', round_nb=99, table_nb=99, scores=[
            {'Hand': 1, 'Value': 20, 'Winner': 1, 'Discarder': 2, 'Confidence': 'certain'},
        ])
        assert _prefill(client_, job_id='job_ghost').status_code == 404
        # No phantom hands, and no sheet to show up in the publisher's overview.
        assert not Hand.objects.filter(tenant=tenant, round_nb=99).exists()
        assert not ScoreSheet.objects.filter(tenant=tenant, round_nb=99).exists()

    def test_a_validated_sheet_survives_a_rescan(self, client_, tournament, scorer,
                                                 finished_job):
        """A table validated with nothing played passes the filled-table gate, so a
        later scan reaches the sheet write. Validation is the scorer's review; the
        scan must only ever raise that flag."""
        tenant = tournament['tenant']
        sheet = ScoreSheet.objects.create(tenant=tenant, round_nb=3, table_nb=1,
                                          validated=True)
        finished_job('job_rescan', round_nb=3, table_nb=1, scores=[
            {'Hand': 1, 'Value': 20, 'Winner': 1, 'Discarder': 2, 'Confidence': 'certain'},
        ])
        # Anonymous, so `validate` could not be honoured even if it were asked for.
        assert _prefill(client_, job_id='job_rescan').status_code == 200
        sheet.refresh_from_db()
        assert sheet.validated is True


class TestScanGateAndVersionBump:
    """scan_prefill's emptiness gate and its writes are one transaction, with the
    table's hand rows locked — the gate is the whole of its conflict handling
    (see _write_hand's docstring). What the version bump buys is the *other*
    direction: a per-cell editor who read the row before the scan fails their own
    version check afterwards. True lock contention can't be exercised on the
    sqlite test database; these pin the two halves that are deterministic."""

    def test_prefill_refuses_a_table_that_gained_a_hand(self, client_, tournament,
                                                        scorer, finished_job):
        """The sheet was opened (16 placeholder rows) and a scorer entered one
        hand before the scan landed: the gate must see that typed hand among the
        placeholders, refuse the whole scan, and write none of its rows."""
        client_.force_login(scorer)
        tenant = tournament['tenant']
        client_.get('/scores_per_hand_3_1')  # materializes the placeholder rows
        typed = Hand.objects.get(tenant=tenant, round_nb=3, table_nb=1, hand_nb=5)
        resp = client_.post('/update_hand_points', {
            'id': typed.id, 'version': typed.version, 'points': 30, 'by': 2, 'from': 4})
        assert resp.status_code == 200

        finished_job('job_gate', round_nb=3, table_nb=1, scores=[
            {'Hand': 1, 'Value': 999, 'Winner': 1, 'Discarder': 2, 'Confidence': 'certain'},
            {'Hand': 5, 'Value': 999, 'Winner': 1, 'Discarder': 2, 'Confidence': 'certain'},
        ])
        resp = _prefill(client_, job_id='job_gate')
        assert resp.status_code == 409
        assert resp.json()['conflict'] is True

        typed.refresh_from_db()
        assert typed.points == 30  # the scorer's hand is intact
        untouched = Hand.objects.get(tenant=tenant, round_nb=3, table_nb=1, hand_nb=1)
        assert untouched.win_by is None  # and no scan row landed either

    def test_prefill_bump_invalidates_a_stale_per_cell_save(self, client_, tournament,
                                                            scorer, finished_job):
        """A scorer's device reads the empty sheet, then the scan prefills it.
        The device's save, still carrying the pre-scan version, must get a 409
        and rebase — never overwrite the prefill silently."""
        client_.force_login(scorer)
        tenant = tournament['tenant']
        client_.get('/scores_per_hand_3_1')
        cell = Hand.objects.get(tenant=tenant, round_nb=3, table_nb=1, hand_nb=1)
        pre_scan_version = cell.version

        finished_job('job_bump', round_nb=3, table_nb=1, scores=[
            {'Hand': 1, 'Value': 20, 'Winner': 1, 'Discarder': 2, 'Confidence': 'certain'}])
        assert _prefill(client_, job_id='job_bump').status_code == 200
        cell.refresh_from_db()
        assert cell.version == pre_scan_version + 1
        assert cell.points == 20

        stale = client_.post('/update_hand_points', {
            'id': cell.id, 'version': pre_scan_version, 'points': 55, 'by': 3, 'from': 1})
        assert stale.status_code == 409
        cell.refresh_from_db()
        assert cell.points == 20  # the prefill survived


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

        img = SimpleUploadedFile('sheet.png', REAL_IMAGE, content_type='image/png')
        resp = client_.post('/scan_2_3', {'image': img})

        assert resp.status_code == 200
        body = resp.json()
        assert body['ok'] is True and body['job_id'] == 'job-abc'
        # The view does no OCR itself — it only hands the bytes to the queue.
        assert captured['bytes'] == REAL_IMAGE
        assert captured['job']['job_id'] == 'job-abc'
        assert captured['job']['round_nb'] == 2 and captured['job']['table_nb'] == 3

    def test_scan_without_image_is_400(self, client_, tournament, scorer):
        client_.force_login(scorer)
        resp = client_.post('/scan', {})
        assert resp.status_code == 400
        assert resp.json()['ok'] is False

    def test_scan_of_an_unseated_table_is_404(self, client_, tournament, scorer,
                                              monkeypatch):
        """A hand-typed /scan_99_99 must not stage the photo: the fixture's chart
        stops at round 3 table 4, so those coordinates are nobody's table, and OCR
        on them costs a vision call for a result that can never be written."""
        client_.force_login(scorer)
        staged = []
        monkeypatch.setattr('mahj.scan_queue.stage_image',
                            lambda raw: staged.append(raw) or 'job-x')

        img = SimpleUploadedFile('sheet.png', REAL_IMAGE, content_type='image/png')
        assert client_.post('/scan_99_99', {'image': img}).status_code == 404
        assert staged == []


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


class TestUploadMetering:
    """Every accepted photo stages a file on a shared volume and buys one paid vision
    call, and the endpoint is anonymous. So it needs its own ceiling — nginx's body
    limit is 20 MB and says nothing about how many."""

    def _upload(self, client, content=REAL_IMAGE, name='sheet.png'):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return client.post('/scan_3_1',
                           {'image': SimpleUploadedFile(name, content, 'image/png')})

    @pytest.fixture
    def staged(self, monkeypatch):
        """Capture what gets staged, without touching Redis or the disk."""
        calls = {'staged': [], 'enqueued': []}
        monkeypatch.setattr('mahj.scan_queue.stage_image',
                            lambda raw: (calls['staged'].append(raw), 'jid')[1])
        monkeypatch.setattr('mahj.scan_queue.enqueue',
                            lambda job: calls['enqueued'].append(job))
        monkeypatch.setattr('mahj.scan_queue.sweep_stale_images', lambda: 0)
        return calls

    def test_an_oversized_photo_is_refused_before_staging(self, client_, tournament,
                                                          staged):
        from mahj.views.scan import MAX_UPLOAD_BYTES
        resp = self._upload(client_, content=b'x' * (MAX_UPLOAD_BYTES + 1))
        assert resp.status_code == 413
        assert staged['staged'] == [], 'an oversized upload must not be staged'
        assert staged['enqueued'] == [], 'and must not buy an OCR call'

    def test_a_normal_photo_is_staged_with_its_table(self, client_, tournament, staged):
        resp = self._upload(client_)
        assert resp.status_code == 200
        assert resp.json()['job_id'] == 'jid'
        # The round and table come from the URL, which is what makes them trustworthy
        # later — see TestScanPrefillTrustsOnlyTheJob.
        job = staged['enqueued'][0]
        assert job['round_nb'] == 3 and job['table_nb'] == 1
        assert job['subdomain'] == 'test'

    def test_a_non_image_is_refused(self, client_, tournament, staged, monkeypatch):
        monkeypatch.setattr('mahj.views.scan._looks_like_an_image', lambda raw: False)
        resp = self._upload(client_, content=b'this is not a photo')
        assert resp.status_code == 400
        assert staged['staged'] == []

    def test_the_image_check_fails_open_without_the_imaging_stack(self):
        """These endpoints are meant to work on a host with no OCR stack, so a
        missing cv2 must not reject every upload."""
        import builtins
        from mahj.views.scan import _looks_like_an_image
        real_import = builtins.__import__

        def no_cv2(name, *a, **kw):
            if name in ('cv2', 'numpy'):
                raise ImportError(name)
            return real_import(name, *a, **kw)

        builtins.__import__ = no_cv2
        try:
            assert _looks_like_an_image(b'anything') is True
        finally:
            builtins.__import__ = real_import

    def test_a_burst_from_one_device_is_throttled(self, client_, tournament, staged,
                                                  settings):
        """One phone (or one script) must not be able to run up the OCR bill."""
        from mahj.views.scan import UPLOAD_MAX_PER_WINDOW
        settings.CACHES = {
            'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
        from django.core.cache import cache
        cache.clear()

        codes = [self._upload(client_).status_code
                 for _ in range(UPLOAD_MAX_PER_WINDOW + 2)]
        assert codes[:UPLOAD_MAX_PER_WINDOW] == [200] * UPLOAD_MAX_PER_WINDOW
        assert codes[UPLOAD_MAX_PER_WINDOW:] == [429, 429]

    def test_throttling_fails_open_without_a_usable_cache(self, client_, tournament,
                                                          staged):
        """The suite's DummyCache can't count. Scanning at the venue must not stop
        because the cache backend won't support it."""
        for _ in range(10):
            assert self._upload(client_).status_code == 200


class TestStaleImageSweep:
    """Staged images are unlinked when a worker picks the job up, so what's left is
    from jobs whose worker died or that were never dequeued. Anonymous uploads land
    here, so without a sweep a run of failures grows the volume until it's full."""

    def test_it_removes_old_files_and_keeps_fresh_ones(self, tmp_path, monkeypatch):
        import os
        import time
        from mahj import scan_queue
        monkeypatch.setattr(scan_queue, 'JOBS_DIR', tmp_path)

        old = tmp_path / 'ancient'
        old.write_bytes(b'x')
        stale = time.time() - scan_queue.STALE_IMAGE_AGE_S - 60
        os.utime(old, (stale, stale))
        fresh = tmp_path / 'recent'
        fresh.write_bytes(b'x')

        assert scan_queue.sweep_stale_images() == 1
        assert not old.exists()
        assert fresh.exists()

    def test_a_missing_directory_is_not_an_error(self, tmp_path, monkeypatch):
        from mahj import scan_queue
        monkeypatch.setattr(scan_queue, 'JOBS_DIR', tmp_path / 'nope')
        assert scan_queue.sweep_stale_images() == 0


class TestResultWriteSurvivesABusBlip:
    """A worker reaches the result write having already paid for the OCR call. An
    unguarded write loses that answer to a momentary outage — and takes the worker
    loop down with it, stalling every job behind."""

    def test_it_retries_and_succeeds(self, monkeypatch):
        import redis as redis_mod
        from mahj import queue_util
        monkeypatch.setattr(queue_util.time, 'sleep', lambda s: None)
        attempts = {'n': 0}

        def flaky():
            attempts['n'] += 1
            if attempts['n'] < 3:
                raise redis_mod.RedisError('bus restarting')

        assert queue_util.write_with_retry(flaky) is True
        assert attempts['n'] == 3

    def test_it_gives_up_without_raising(self, monkeypatch):
        import redis as redis_mod
        from mahj import queue_util
        monkeypatch.setattr(queue_util.time, 'sleep', lambda s: None)

        def always_down():
            raise redis_mod.RedisError('bus gone')

        assert queue_util.write_with_retry(always_down) is False

    def test_a_worker_logs_the_loss_instead_of_dying(self, monkeypatch, caplog):
        """The whole point: one unwritable result must not stop the loop."""
        import logging
        from mahj import scan_queue
        from mahj.management.commands.scan_worker import Command
        monkeypatch.setattr(scan_queue, 'set_result_with_retry', lambda *a, **k: False)
        monkeypatch.setattr('mahj.views.scan._read_image',
                            lambda path: (_ for _ in ()).throw(RuntimeError('no file')))
        monkeypatch.setattr(scan_queue, 'discard_image', lambda jid: None)
        with caplog.at_level(logging.ERROR):
            Command()._process({'job_id': 'j', 'round_nb': 1, 'table_nb': 1})
        assert any('could not be stored' in r.message for r in caplog.records)
