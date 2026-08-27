"""Each tournament aligns photos to its own score sheet.

The sheet used to be one file committed to the repo, so a club whose paper looked
different got "could not align" on every photo, forever, with a message that
blamed the photographer. That failure is silent — nothing logs, nothing is billed,
nobody at the venue can tell configuration from bad lighting — which is why most
of what is pinned here is *diagnosis*: which template a job used, what an
organiser is told when it fails, and that the alignment test they use to check it
never costs anything.

The tests that need real ORB matching are skipped where `cv2` isn't installed
(the working dev venv); they run in CI and in the image. Everything about
routing, caching and validation runs everywhere.
"""
import sys
import types

import pytest

from mahj import scan_key
from mahj.models import ScanConfig
from mahj.tests.conftest import client_for, reauth, role_user
from mahj.views import scan as scanview

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clean_template_cache():
    """The ORB cache is module state that outlives a test, so start each one empty."""
    scanview._TPL_CACHE.clear()
    yield
    scanview._TPL_CACHE.clear()


def _stub_templates(monkeypatch):
    """Replace ORB construction with a marker, so the caching and routing logic can
    be tested on hosts with no OpenCV."""
    built = []

    def fake_build(etag, image_bgr, bbox):
        built.append(etag)
        return scanview.Template(etag, kp=None, des=None, h=100, w=100, bbox=bbox)

    monkeypatch.setattr(scanview, '_build_template', fake_build)
    monkeypatch.setattr(scanview, '_decode_template', lambda raw: object())
    return built


def _a_sheet(etag='sheet', bbox=(0, 0, 10, 10)):
    return scan_key.ScanSetup(key='sk-ant-x', template=b'sheet-bytes',
                              etag=etag, bbox=bbox)


# ---------------------------------------------------------------------------
# Which sheet a job uses
# ---------------------------------------------------------------------------

class TestTemplateResolution:

    def test_no_sheet_means_no_template_at_all(self, monkeypatch):
        """There is no built-in fallback, on purpose. A sheet nobody chose fails
        every photo, silently and permanently, with a message that reads as bad
        photography — so scanning stops instead, the way it does with no key."""
        _stub_templates(monkeypatch)
        assert scanview.resolve_template(scan_key.ScanSetup(key='sk-ant-x')) is None
        assert scanview.resolve_template(None) is None

    def test_the_app_ships_no_usable_fallback_sheet(self):
        """The example sheet is a download for organisers to print, never a
        template the code reaches for on its own."""
        assert not hasattr(scanview, 'builtin_template')
        assert scanview.EXAMPLE_SHEET_PATH.exists()

    def test_a_tenants_own_sheet_is_used(self, monkeypatch):
        _stub_templates(monkeypatch)
        tpl = scanview.resolve_template(_a_sheet('abc123', (1, 2, 30, 40)))
        assert tpl.etag == 'abc123' and tpl.bbox == (1, 2, 30, 40)

    def test_two_tenants_in_one_worker_each_get_their_own(self, monkeypatch):
        """The test the old design could not pass: the ORB state lived in module
        globals built once per process, so the first tournament to scan decided
        which sheet every other tournament was matched against."""
        _stub_templates(monkeypatch)
        a = scanview.resolve_template(_a_sheet('aaa', (0, 0, 10, 10)))
        b = scanview.resolve_template(_a_sheet('bbb', (5, 5, 50, 50)))
        assert a.etag == 'aaa' and b.etag == 'bbb'
        assert a.bbox != b.bbox
        # And the first one is still itself afterwards.
        again = scanview.resolve_template(_a_sheet('aaa', (0, 0, 10, 10)))
        assert again.bbox == (0, 0, 10, 10)


class TestTemplateCache:
    """Built once per sheet, keyed by content, and bounded — the workers are
    long-lived processes and one entry per tenant that ever scanned would be a
    slow leak against the memory compose budgets them."""

    def test_the_same_sheet_is_built_once(self, monkeypatch):
        built = _stub_templates(monkeypatch)
        for _ in range(3):
            scanview._template_for('same-etag', b'x', (0, 0, 10, 10))
        assert built == ['same-etag']

    def test_changed_bytes_rebuild(self, monkeypatch):
        built = _stub_templates(monkeypatch)
        scanview._template_for('etag-1', b'x', (0, 0, 10, 10))
        scanview._template_for('etag-2', b'y', (0, 0, 10, 10))
        assert built == ['etag-1', 'etag-2']

    def test_a_moved_box_rebuilds_even_on_the_same_image(self, monkeypatch):
        """The etag covers the image, not the crop — so the box has to be checked
        too, or adjusting it in the admin would have no effect until a restart."""
        built = _stub_templates(monkeypatch)
        scanview._template_for('etag-1', b'x', (0, 0, 10, 10))
        scanview._template_for('etag-1', b'x', (0, 0, 20, 20))
        assert built == ['etag-1', 'etag-1']

    def test_it_never_grows_past_its_cap(self, monkeypatch):
        built = _stub_templates(monkeypatch)
        for i in range(scanview._TPL_CACHE_MAX + 3):
            scanview._template_for(f'etag-{i}', b'x', (0, 0, 10, 10))
        assert len(scanview._TPL_CACHE) == scanview._TPL_CACHE_MAX
        assert built == [f'etag-{i}' for i in range(scanview._TPL_CACHE_MAX + 3)]

    def test_the_oldest_goes_first(self, monkeypatch):
        _stub_templates(monkeypatch)
        for i in range(scanview._TPL_CACHE_MAX):
            scanview._template_for(f'etag-{i}', b'x', (0, 0, 10, 10))
        # Touch the oldest so it is no longer the least recently used.
        scanview._template_for('etag-0', b'x', (0, 0, 10, 10))
        scanview._template_for('newcomer', b'x', (0, 0, 10, 10))
        assert 'etag-0' in scanview._TPL_CACHE
        assert 'etag-1' not in scanview._TPL_CACHE


# ---------------------------------------------------------------------------
# Failure, and what the organiser is told about it
# ---------------------------------------------------------------------------

class TestAlignmentFailureIsDiagnosable:

    def test_the_message_points_at_the_sheet_too(self, monkeypatch):
        """The old wording ("try a clearer, less tilted shot") blamed the
        photographer for what is just as likely the wrong sheet uploaded."""
        _stub_templates(monkeypatch)
        monkeypatch.setattr(scanview, '_align_to_template', lambda img, tpl: None)
        tpl = scanview.resolve_template(_a_sheet())
        _, err = scanview.align_and_crop(object(), tpl)
        assert err['kind'] == 'align'
        assert 'score sheet setup' in err['error']

    def test_a_failure_reaches_the_tenants_own_page(self, tournament, monkeypatch):
        """Nobody reads the worker log during an event."""
        from mahj.management.commands import scan_worker
        cfg = ScanConfig.objects.create(tenant=tournament['tenant'],
                                        api_key_enc=scan_key.encrypt('sk-ant-x'))
        scan_worker.Command._record('test', {'status': 'error', 'kind': 'align',
                                             'error': 'whatever the player saw'})
        cfg.refresh_from_db()
        assert 'the one your tables use' in cfg.last_error
        assert cfg.last_error_at is not None

    def test_a_rejected_key_is_recorded_and_the_grant_dropped(self, tournament):
        from mahj.management.commands import scan_worker
        cfg = ScanConfig.objects.create(tenant=tournament['tenant'],
                                        api_key_enc=scan_key.encrypt('sk-ant-x'))
        scan_worker.Command._record('test', {'status': 'error', 'kind': 'auth',
                                             'error': 'misconfigured'})
        cfg.refresh_from_db()
        assert 'rejected' in cfg.last_error

    def test_a_successful_scan_records_nothing(self, tournament):
        from mahj.management.commands import scan_worker
        cfg = ScanConfig.objects.create(tenant=tournament['tenant'])
        scan_worker.Command._record('test', {'status': 'done', 'scores': []})
        cfg.refresh_from_db()
        assert cfg.last_error == ''


class TestABadTemplateFailsOneJobNotTheWorker:

    def test_an_undecodable_sheet_is_that_tenants_problem_alone(self, monkeypatch):
        # (the worker builds each sheet lazily, so this can only fail one job)
        """One tournament's bad upload must not take down a worker that four
        replicas share with every other tournament."""
        monkeypatch.setattr(scanview, '_decode_template', lambda raw: None)
        with pytest.raises(ValueError):
            scanview._template_for('bad', b'not-an-image', (0, 0, 10, 10))

    def test_the_worker_turns_it_into_an_error_result(self, monkeypatch, tmp_path):
        from mahj import scan_queue
        from mahj.management.commands import scan_worker as mod
        cmd = mod.Command()
        monkeypatch.setattr(mod.scan_queue, 'JOBS_DIR', tmp_path)
        monkeypatch.setattr(mod.scan_key, 'resolve_setup',
                            lambda sd: _a_sheet('bad', (0, 0, 5, 5)))
        monkeypatch.setattr(mod.scan_key, 'stamp_error', lambda sd, msg: None)
        monkeypatch.setattr(mod.scanview, '_read_image', lambda p: object())
        monkeypatch.setattr(mod.scanview, 'resolve_template',
                            lambda setup: (_ for _ in ()).throw(ValueError('undecodable')))
        written = {}
        monkeypatch.setattr(mod.scan_queue, 'set_result_with_retry',
                            lambda jid, res: written.update({jid: res}) or True)
        cmd._process({'job_id': 'j1', 'subdomain': 'test'})
        assert written['j1']['status'] == 'error'
        assert 'undecodable' not in written['j1']['error']


# ---------------------------------------------------------------------------
# The alignment test has to stay free
# ---------------------------------------------------------------------------

class TestPreviewCostsNothing:

    @pytest.fixture
    def fake_anthropic(self, monkeypatch):
        module = types.ModuleType('anthropic')

        class Anthropic:
            def __init__(self, **kwargs):
                raise AssertionError('the alignment test must never reach the API')

        module.Anthropic = Anthropic
        for name in ('AuthenticationError', 'PermissionDeniedError', 'RateLimitError',
                     'NotFoundError', 'APIStatusError', 'APIConnectionError',
                     'APITimeoutError'):
            setattr(module, name, type(name, (Exception,), {}))
        monkeypatch.setitem(sys.modules, 'anthropic', module)

    def test_run_preview_returns_a_crop_and_buys_nothing(self, monkeypatch, fake_anthropic):
        _stub_templates(monkeypatch)
        monkeypatch.setattr(scanview, '_align_to_template', lambda img, tpl: 'H')
        monkeypatch.setattr(scanview, '_warp_crop', lambda img, H, bbox: 'crop')
        monkeypatch.setattr(scanview, '_encode_jpeg', lambda crop: 'BASE64')
        result = scanview.run_preview(object(), scanview.resolve_template(_a_sheet()))
        assert result['status'] == 'done'
        assert result['preview'].startswith('data:image/jpeg;base64,')

    def test_a_preview_with_no_sheet_says_so(self, monkeypatch, fake_anthropic):
        """The organiser's first visit: nothing uploaded yet, so there is nothing
        to align against and nothing to guess."""
        result = scanview.run_preview(object(), None)
        assert result['status'] == 'error'
        assert 'blank score sheet' in result['error']

    def test_the_worker_routes_a_preview_job_away_from_the_ocr(self, monkeypatch, tmp_path,
                                                               fake_anthropic):
        from mahj.management.commands import scan_worker as mod
        cmd = mod.Command()
        monkeypatch.setattr(mod.scan_queue, 'JOBS_DIR', tmp_path)
        monkeypatch.setattr(mod.scan_key, 'resolve_setup',
                            lambda sd: scan_key.ScanSetup(key='sk-ant-x'))
        monkeypatch.setattr(mod.scan_key, 'stamp_error', lambda sd, msg: None)
        monkeypatch.setattr(mod.scanview, '_read_image', lambda p: object())
        monkeypatch.setattr(mod.scanview, 'resolve_template', lambda setup: 'TPL')
        called = []
        monkeypatch.setattr(mod.scanview, 'run_scan',
                            lambda *a, **kw: called.append('ocr') or {'status': 'done'})
        monkeypatch.setattr(mod.scanview, 'run_preview',
                            lambda img, tpl: called.append('preview') or {'status': 'done'})
        monkeypatch.setattr(mod.scan_queue, 'set_result_with_retry', lambda jid, res: True)
        cmd._process({'job_id': 'p1', 'subdomain': 'test', 'preview': True})
        assert called == ['preview']

    def test_a_preview_runs_even_for_a_tenant_with_no_key(self, monkeypatch, tmp_path,
                                                          fake_anthropic):
        """Deliberate ordering: an organiser sets the sheet up before paying for a
        key, and the test costs nothing to run."""
        from mahj.management.commands import scan_worker as mod
        cmd = mod.Command()
        monkeypatch.setattr(mod.scan_queue, 'JOBS_DIR', tmp_path)
        monkeypatch.setattr(mod.scan_key, 'resolve_setup', lambda sd: scan_key.ScanSetup())
        monkeypatch.setattr(mod.scan_key, 'stamp_error', lambda sd, msg: None)
        monkeypatch.setattr(mod.scanview, '_read_image', lambda p: object())
        monkeypatch.setattr(mod.scanview, 'resolve_template', lambda setup: 'TPL')
        monkeypatch.setattr(mod.scanview, 'run_preview',
                            lambda img, tpl: {'status': 'done', 'preview': 'data:,'})
        written = {}
        monkeypatch.setattr(mod.scan_queue, 'set_result_with_retry',
                            lambda jid, res: written.update({jid: res}) or True)
        cmd._process({'job_id': 'p2', 'subdomain': 'test', 'preview': True})
        assert written['p2']['status'] == 'done'


# ---------------------------------------------------------------------------
# The admin half
# ---------------------------------------------------------------------------

class TestRehearsalScan:
    """The "Test scan" button: the same read a player buys, on demand.

    It exists because the two cheap checks don't cover the failure that hurts —
    a crop can be pixel-perfect and still read as nonsense, since the prompt
    assumes 16 hands in Value / Winner / Discarder columns. That only shows up
    on a real read, and the venue is the wrong place to discover it.
    """

    @pytest.fixture
    def admin_client(self, tournament):
        c = client_for()
        c.force_login(role_user('boss', tournament['tenant'], admin=True))
        return c

    @pytest.fixture
    def keyed(self, tournament):
        return ScanConfig.objects.create(
            tenant=tournament['tenant'], api_key_enc=scan_key.encrypt('sk-ant-x'),
            key_tail='nt-x')

    @pytest.fixture
    def queued(self, monkeypatch):
        jobs = []
        monkeypatch.setattr('mahj.scan_queue.sweep_stale_images', lambda: 0)
        monkeypatch.setattr('mahj.scan_queue.stage_image', lambda raw: 'jid')
        monkeypatch.setattr('mahj.scan_queue.enqueue', lambda job: jobs.append(job))
        return jobs

    def _photo(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from mahj.tests.test_scan import REAL_IMAGE
        return SimpleUploadedFile('sheet.png', REAL_IMAGE, content_type='image/png')

    def test_a_full_test_needs_a_key_because_it_spends(self, admin_client, tournament,
                                                      queued):
        resp = admin_client.post('/scan_template_preview',
                                 {'photo': self._photo(), 'full': '1'})
        assert resp.status_code == 400
        assert 'API key' in resp.json()['error']
        assert queued == [], 'nothing may be queued when there is nothing to pay with'

    def test_the_free_alignment_test_needs_no_key(self, admin_client, tournament, queued):
        """Deliberate: an organiser sets the sheet up before paying for anything."""
        resp = admin_client.post('/scan_template_preview', {'photo': self._photo()})
        assert resp.status_code == 200
        assert queued[0]['preview'] is True

    def test_a_full_test_queues_a_real_read(self, admin_client, tournament, keyed, queued):
        resp = admin_client.post('/scan_template_preview',
                                 {'photo': self._photo(), 'full': '1'})
        assert resp.status_code == 200
        assert queued[0]['preview'] is False
        assert queued[0]['rehearsal'] is True

    def test_a_rehearsal_can_never_write_to_a_score_sheet(self, admin_client, tournament,
                                                          keyed, queued):
        """It carries no round and no table, and scan_prefill refuses a job with
        no table — so the read is a read, whatever the client later claims."""
        admin_client.post('/scan_template_preview', {'photo': self._photo(), 'full': '1'})
        assert queued[0]['round_nb'] is None and queued[0]['table_nb'] is None

    def test_a_rehearsals_failure_is_not_stamped_on_the_page(self, monkeypatch):
        """The organiser is looking straight at the result. Recording it would
        leave a warning on their own page about a photo they took on purpose."""
        from mahj.management.commands import scan_worker as mod
        cmd = mod.Command()
        recorded = []
        monkeypatch.setattr(cmd, '_record', lambda *a: recorded.append(a))
        monkeypatch.setattr(mod.scan_key, 'resolve_setup',
                            lambda sd: _a_sheet())
        monkeypatch.setattr(mod.scan_queue, 'set_result_with_retry', lambda jid, res: True)
        monkeypatch.setattr(mod.scanview, '_read_image', lambda p: object())
        monkeypatch.setattr(mod.scanview, 'resolve_template', lambda setup: 'TPL')
        monkeypatch.setattr(mod.scanview, 'run_scan',
                            lambda *a, **kw: {'status': 'error', 'kind': 'align',
                                              'error': 'nope'})
        cmd._process({'job_id': 'r1', 'subdomain': 'test', 'rehearsal': True})
        assert recorded == []
        cmd._process({'job_id': 'r2', 'subdomain': 'test'})
        assert len(recorded) == 1, 'a real scan failing must still be recorded' 

    def test_the_poll_hands_back_what_was_read(self, admin_client, tournament, monkeypatch):
        monkeypatch.setattr('mahj.scan_queue.get_result',
                            lambda jid: {'status': 'done', 'subdomain': 'test',
                                         'scores': [{'Hand': 1, 'Value': 88}]})
        body = admin_client.get('/scan_template_preview_status?job_id=x').json()
        assert body['status'] == 'ok'
        assert body['scores'] == [{'Hand': 1, 'Value': 88}]


class TestTheOcrRequestItself:
    """The request's own shape, pinned.

    All of this is here because of one live incident: bumping the model to one
    that thinks by default silently made thinking share `max_tokens` with the
    answer, so the JSON came back truncated — sometimes mid-string, sometimes
    with no answer at all — and every failure reached the player as "could not
    read the sheet, re-take the photo".
    """

    @pytest.fixture
    def captured(self, monkeypatch):
        """A fake SDK that records the request and returns whatever we set."""
        sent = {}
        reply = {'stop_reason': 'end_turn',
                 'text': '{"Scores": [{"Hand": 1, "Value": 88, "Winner": 1, '
                         '"Discarder": 2, "Confidence": "certain"}]}'}
        module = types.ModuleType('anthropic')

        class Block:
            type = 'text'

            def __init__(self, text):
                self.text = text

        class Message:
            def __init__(self):
                self.stop_reason = reply['stop_reason']
                self.content = ([Block(reply['text'])] if reply['text'] is not None
                                else [types.SimpleNamespace(type='thinking')])

        class Messages:
            def create(self, **kwargs):
                sent.update(kwargs)
                return Message()

        class Anthropic:
            def __init__(self, **kwargs):
                self.messages = Messages()

        module.Anthropic = Anthropic
        for name in ('AuthenticationError', 'PermissionDeniedError', 'RateLimitError',
                     'NotFoundError', 'APIStatusError', 'APIConnectionError',
                     'APITimeoutError'):
            setattr(module, name, type(name, (Exception,), {}))
        monkeypatch.setitem(sys.modules, 'anthropic', module)
        monkeypatch.setattr(scanview, '_align_to_template', lambda img, tpl: 'H')
        monkeypatch.setattr(scanview, '_warp_crop', lambda img, H, bbox: 'crop')
        monkeypatch.setattr(scanview, '_encode_jpeg', lambda crop: 'BASE64')
        return sent, reply

    def _run(self, monkeypatch):
        _stub_templates(monkeypatch)
        tpl = scanview.resolve_template(_a_sheet())
        return scanview.run_scan(object(), 'sk-ant-x', tpl)

    def test_thinking_is_disabled(self, captured, monkeypatch):
        """Transcribing digits is not a reasoning task, and thinking tokens bill
        to the tenant at output rates on every scan of the event."""
        sent, _ = captured
        self._run(monkeypatch)
        assert sent['thinking'] == {'type': 'disabled'}

    def test_the_budget_leaves_room_for_sixteen_rows(self, captured, monkeypatch):
        sent, _ = captured
        self._run(monkeypatch)
        assert sent['max_tokens'] >= 4096

    def test_a_truncated_answer_is_not_reported_as_a_bad_photo(self, captured,
                                                               monkeypatch):
        """The failure that shipped: hitting the cap surfaced as "re-take the
        photo", sending an organiser to fix lighting for a token-budget problem."""
        sent, reply = captured
        reply['stop_reason'] = 'max_tokens'
        result = self._run(monkeypatch)
        assert result['status'] == 'error'
        assert result['kind'] == 'truncated'

    def test_an_answerless_response_does_not_crash_the_worker(self, captured,
                                                              monkeypatch):
        """The other half of the same incident: the whole budget went to thinking
        and the response carried no text block at all."""
        sent, reply = captured
        reply['text'] = None
        result = self._run(monkeypatch)
        assert result['status'] == 'error'

    def test_a_good_response_still_reads(self, captured, monkeypatch):
        result = self._run(monkeypatch)
        assert result['status'] == 'done'
        assert result['scores'][0]['Value'] == 88

    def test_the_model_and_the_test_button_cannot_drift(self):
        """A key that tests green against one model and scans with another is a
        failure that only appears at the venue."""
        import inspect
        from mahj.views import scan_admin
        assert 'OCR_MODEL' in inspect.getsource(scan_admin.scan_key_test)


class TestTemplateAdmin:

    @pytest.fixture
    def admin_client(self, tournament):
        c = client_for()
        c.force_login(role_user('boss', tournament['tenant'], admin=True))
        return reauth(c)

    def _png(self, name='sheet.png'):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from mahj.tests.test_scan import REAL_IMAGE
        return SimpleUploadedFile(name, REAL_IMAGE, content_type='image/png')

    def test_a_box_with_no_sheet_is_refused(self, admin_client):
        resp = admin_client.post('/scan_template_save',
                                 {'bbox_x1': 0, 'bbox_y1': 0, 'bbox_x2': 5, 'bbox_y2': 5})
        assert resp.status_code == 400
        assert 'blank score sheet' in resp.json()['error']

    def test_clearing_the_sheet_turns_scanning_off(self, admin_client, tournament):
        cfg = ScanConfig.objects.create(tenant=tournament['tenant'],
                                        template_img=b'x', template_etag='e',
                                        bbox_x1=1, bbox_y1=1, bbox_x2=9, bbox_y2=9)
        assert cfg.has_template
        resp = admin_client.post('/scan_template_save', {'clear_template': '1'})
        assert resp.status_code == 200
        cfg.refresh_from_db()
        assert not cfg.has_template
        assert scan_key.resolve_setup('test').template is None
        assert scan_key.is_configured('test') is False

    def test_half_a_setup_is_no_setup(self, tournament):
        """A stored image with no box reads nothing, so it must not count as a
        sheet — a half-saved upload has to leave scanning off, not half-on."""
        cfg = ScanConfig.objects.create(tenant=tournament['tenant'], template_img=b'x')
        assert cfg.bbox is None and not cfg.has_template
        assert scan_key.resolve_setup('test').template is None

    def test_an_inside_out_box_is_refused(self, tournament):
        from mahj.views.scan_admin import _parse_bbox
        bbox, err = _parse_bbox({'bbox_x1': '90', 'bbox_y1': '0',
                                 'bbox_x2': '10', 'bbox_y2': '50'}, 100, 100)
        assert bbox is None and 'empty' in err

    def test_a_box_off_the_sheet_is_refused(self, tournament):
        from mahj.views.scan_admin import _parse_bbox
        bbox, err = _parse_bbox({'bbox_x1': '0', 'bbox_y1': '0',
                                 'bbox_x2': '500', 'bbox_y2': '50'}, 100, 100)
        assert bbox is None and 'outside' in err

    def test_a_missing_box_is_refused(self, tournament):
        from mahj.views.scan_admin import _parse_bbox
        bbox, err = _parse_bbox({}, 100, 100)
        assert bbox is None and err

    def test_a_good_box_survives(self, tournament):
        from mahj.views.scan_admin import _parse_bbox
        bbox, err = _parse_bbox({'bbox_x1': '1', 'bbox_y1': '2',
                                 'bbox_x2': '30', 'bbox_y2': '40'}, 100, 100)
        assert bbox == (1, 2, 30, 40) and err is None

    def test_an_oversized_sheet_is_refused(self, admin_client, monkeypatch):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from mahj.views import scan_admin
        big = SimpleUploadedFile('sheet.png',
                                 b'x' * (scan_admin.MAX_TEMPLATE_BYTES + 1), 'image/png')
        resp = admin_client.post('/scan_template_save',
                                 {'template': big, 'bbox_x1': 0, 'bbox_y1': 0,
                                  'bbox_x2': 5, 'bbox_y2': 5})
        assert resp.status_code == 400
        assert 'too large' in resp.json()['error']

    def test_a_lapsed_password_confirmation_does_not_lose_the_upload(self, tournament):
        """Setting a sheet up takes longer than the 10-minute reauth window —
        upload, drag a box, test the alignment, fetch a key from another tab. A
        403 at Save would throw all of it away, so the sheet endpoints are gated
        on being a tenant admin and nothing more. The key endpoints still reauth;
        the page's JS re-prompts and retries for those.
        """
        c = client_for()
        c.force_login(role_user('boss', tournament['tenant'], admin=True))
        # Deliberately NOT reauthed.
        cfg = ScanConfig.objects.create(tenant=tournament['tenant'],
                                        template_img=b'x', template_etag='e',
                                        bbox_x1=1, bbox_y1=1, bbox_x2=9, bbox_y2=9)
        # Re-drawing the box is refused for its own reasons on a host with no
        # imaging stack, but it must never be refused for a lapsed confirmation.
        resp = c.post('/scan_template_save',
                      {'bbox_x1': 2, 'bbox_y1': 2, 'bbox_x2': 8, 'bbox_y2': 8})
        assert resp.status_code != 403
        assert resp.json().get('status') != 'reauth_required'
        # Clearing needs no OpenCV, so it can be asserted end-to-end anywhere.
        assert c.post('/scan_template_save', {'clear_template': '1'}).status_code == 200
        cfg.refresh_from_db()
        assert not cfg.has_template

    def test_the_key_endpoints_still_require_a_recent_password(self, tournament):
        """The other half of that trade: a credential is not configuration."""
        c = client_for()
        c.force_login(role_user('boss', tournament['tenant'], admin=True))
        resp = c.post('/scan_key_save', {'api_key': 'sk-ant-x'})
        assert resp.status_code == 403
        assert resp.json()['status'] == 'reauth_required'
        assert scan_key.resolve_key('test') == ''

    def test_a_scorer_cannot_change_the_sheet(self, tournament):
        c = client_for()
        c.force_login(role_user('sam', tournament['tenant'], scorer=True))
        assert c.post('/scan_template_save', {'clear_template': '1'}).status_code in (403, 302)

    def test_the_stored_sheet_is_not_public(self, client_, tournament):
        ScanConfig.objects.create(tenant=tournament['tenant'], template_img=b'x',
                                  template_etag='e', bbox_x2=9, bbox_y2=9)
        assert client_.get('/scan_template_image').status_code in (403, 302, 404)

    def test_the_page_offers_the_example_box_as_a_starting_point(self, admin_client):
        content = admin_client.get('/admin?page=scanning').context['page_content']
        assert str(scanview.EXAMPLE_SHEET_BBOX[3]) in content

    def test_a_preview_of_another_tenants_job_is_not_readable(self, admin_client,
                                                              monkeypatch):
        monkeypatch.setattr('mahj.scan_queue.get_result',
                            lambda jid: {'status': 'done', 'subdomain': 'other',
                                         'preview': 'data:image/jpeg;base64,SECRET'})
        body = admin_client.get('/scan_template_preview_status?job_id=x').json()
        assert 'SECRET' not in str(body)


# ---------------------------------------------------------------------------
# The real thing, where OpenCV exists
# ---------------------------------------------------------------------------

class TestRealAlignment:
    """End-to-end through actual ORB matching. Skipped where cv2 isn't installed
    (the dev venv); these run in CI and in the built image."""

    @pytest.fixture(autouse=True)
    def needs_cv2(self):
        pytest.importorskip('cv2')
        pytest.importorskip('numpy')

    def _example_template(self):
        raw = scanview.EXAMPLE_SHEET_PATH.read_bytes()
        return scanview.resolve_template(
            scan_key.ScanSetup(key='sk-ant-x', template=raw, etag='example',
                               bbox=scanview.EXAMPLE_SHEET_BBOX))

    def test_a_sheet_aligns_to_itself(self):
        """A photo *is* the template: alignment must succeed."""
        import cv2
        image = cv2.imread(str(scanview.EXAMPLE_SHEET_PATH))
        b64, err = scanview.align_and_crop(image, self._example_template())
        assert err is None, err
        assert b64

    def test_a_photo_of_nothing_does_not_align(self):
        import numpy as np
        blank = np.zeros((800, 600, 3), dtype=np.uint8)
        _, err = scanview.align_and_crop(blank, self._example_template())
        assert err is not None and err['kind'] == 'align'

    def test_a_tenant_sheet_is_built_from_its_stored_bytes(self):
        """The whole point: bytes out of a database row become a working template."""
        raw = scanview.EXAMPLE_SHEET_PATH.read_bytes()
        setup = scan_key.ScanSetup(key='sk-ant-x', template=raw, etag='real',
                                   bbox=scanview.EXAMPLE_SHEET_BBOX)
        tpl = scanview.resolve_template(setup)
        assert tpl.des is not None

        import cv2
        image = cv2.imread(str(scanview.EXAMPLE_SHEET_PATH))
        b64, err = scanview.align_and_crop(image, tpl)
        assert err is None and b64

    def test_a_featureless_sheet_is_rejected_at_upload_time(self):
        """Better to refuse it on the admin page than to accept a sheet that can
        never match anything and let it fail silently at the venue."""
        import cv2
        import numpy as np
        blank = np.full((400, 300, 3), 255, dtype=np.uint8)
        with pytest.raises(ValueError):
            scanview._build_template('flat', blank, (0, 0, 100, 100))
