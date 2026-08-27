"""Scanning is bring-your-own-key: what a tenant with no key of its own can spend.

The answer has to be *nothing*, and it has to be nothing at the earliest possible
point — before an 8 MB body is read, before a file is staged on the shared
volume, before a job is queued. The scan page is anonymous by design, so the gate
is the only thing standing between a stranger with a link and somebody's bill.

These tests avoid the OCR stack entirely (the working venv has neither `cv2` nor
`anthropic`): they exercise resolution, the gate and the admin views, and inject
a fake `anthropic` module where an SDK shape is needed.
"""
import sys
import types
from pathlib import Path

import pytest
from django.contrib.auth.models import User

from mahj import scan_key
from mahj.models import ScanConfig, Tenant
from mahj.tests.conftest import HOST_B, client_for, reauth, role_user

pytestmark = pytest.mark.django_db


SHEET = dict(template_img=b'a stand-in for the sheet image', template_etag='sheet-etag',
             bbox_x1=1, bbox_y1=2, bbox_x2=30, bbox_y2=40)


@pytest.fixture
def keyed(tournament):
    """A key, and no sheet — which is *not* enough to scan."""
    return ScanConfig.objects.create(
        tenant=tournament['tenant'],
        api_key_enc=scan_key.encrypt('sk-ant-test-key-9fA2'), key_tail='9fA2')


@pytest.fixture
def scannable(tournament):
    """Both halves: a key to pay with and a sheet to match against."""
    return ScanConfig.objects.create(
        tenant=tournament['tenant'],
        api_key_enc=scan_key.encrypt('sk-ant-test-key-9fA2'), key_tail='9fA2', **SHEET)


@pytest.fixture
def watched_queue(monkeypatch):
    """Everything the money gate must prevent, in one place."""
    calls = {'staged': [], 'enqueued': []}
    monkeypatch.setattr('mahj.scan_queue.stage_image',
                        lambda raw: (calls['staged'].append(raw), 'jid')[1])
    monkeypatch.setattr('mahj.scan_queue.enqueue',
                        lambda job: calls['enqueued'].append(job))
    monkeypatch.setattr('mahj.scan_queue.sweep_stale_images', lambda: 0)
    return calls


def _photo():
    from django.core.files.uploadedfile import SimpleUploadedFile
    from mahj.tests.test_scan import REAL_IMAGE
    return SimpleUploadedFile('sheet.png', REAL_IMAGE, content_type='image/png')


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

class TestNoKeyNoSpend:

    def test_a_keyless_tenant_stages_nothing_and_queues_nothing(self, client_, tournament,
                                                                watched_queue):
        """The test that actually encodes "cannot spend money"."""
        resp = client_.post('/scan_3_1', {'image': _photo()})
        assert resp.status_code == 503
        assert watched_queue['staged'] == [], 'no image may be staged'
        assert watched_queue['enqueued'] == [], 'and no OCR call may be bought'

    def test_the_refusal_names_nothing_a_stranger_should_learn(self, client_, tournament):
        """This body reaches anyone who can reach /scan, which is everyone."""
        body = client_.post('/scan_3_1', {'image': _photo()}).content.decode()
        for leak in ('ANTHROPIC', 'anthropic', 'api_key', 'sk-ant', 'API key', 'env'):
            assert leak not in body, f'{leak!r} must not reach an anonymous client'

    def test_the_gate_precedes_the_size_limit(self, client_, tournament, watched_queue):
        """An unkeyed tenant must not have a huge body read in before being refused.

        413 here would mean the size check ran first — which is the wrong order,
        because it means the request was already in memory.
        """
        from mahj.views.scan import MAX_UPLOAD_BYTES
        from django.core.files.uploadedfile import SimpleUploadedFile
        huge = SimpleUploadedFile('sheet.png', b'x' * (MAX_UPLOAD_BYTES + 1), 'image/png')
        assert client_.post('/scan_3_1', {'image': huge}).status_code == 503

    def test_a_fully_configured_tenant_scans(self, client_, tournament, scannable,
                                             watched_queue):
        resp = client_.post('/scan_3_1', {'image': _photo()})
        assert resp.status_code == 200
        assert len(watched_queue['enqueued']) == 1

    def test_a_key_without_a_sheet_is_not_enough(self, client_, tournament, keyed,
                                                 watched_queue):
        """No fallback sheet, deliberately. Guessing one fails every photo
        silently and forever, and tells the player their photography is at
        fault — so an unconfigured sheet stops scanning exactly as a missing key
        does, while the organiser can still see which half is missing."""
        resp = client_.post('/scan_3_1', {'image': _photo()})
        assert resp.status_code == 503
        assert watched_queue['staged'] == [] and watched_queue['enqueued'] == []

    def test_a_sheet_without_a_key_is_not_enough_either(self, client_, tournament,
                                                        watched_queue):
        ScanConfig.objects.create(tenant=tournament['tenant'], **SHEET)
        assert client_.post('/scan_3_1', {'image': _photo()}).status_code == 503
        assert watched_queue['enqueued'] == []

    def test_the_page_still_renders_with_an_explanation(self, client_, tournament):
        """A player following a QR from a printed sheet gets a sentence, not a 404."""
        resp = client_.get('/scan_3_1')
        assert resp.status_code == 200
        html = resp.content.decode()
        assert 'is not switched on for this tournament' in html
        # And no scan UI at all: the JS binds inputs that aren't there.
        assert 'Tap to take photo' not in html

    def test_an_organiser_is_told_where_to_fix_it(self, client_, tournament):
        admin = role_user('boss', tournament['tenant'], admin=True)
        client_.force_login(admin)
        html = client_.get('/scan').content.decode()
        assert 'page=scanning' in html

    def test_a_player_is_not(self, client_, tournament):
        assert 'page=scanning' not in client_.get('/scan').content.decode()


class TestSetupHomeOptionalSection:
    """The Setup checklist gained an Optional section for the two features a
    tournament can run without. It is separate from the checklist because an
    unticked step reads as work still to do, and neither of these is."""

    @pytest.fixture
    def admin_client(self, tournament):
        c = client_for()
        c.force_login(role_user('boss', tournament['tenant'], admin=True))
        return c

    def _page(self, client):
        resp = client.get('/admin?page=setup')
        assert resp.status_code == 200
        return resp.context['page_content']

    def test_nothing_configured_says_so_without_looking_like_a_todo(self, admin_client):
        from mahj.tests.conftest import has_testid
        html = self._page(admin_client)
        assert has_testid(html, 'optional-features')
        assert has_testid(html, 'scanning-not-configured')
        assert has_testid(html, 'publish-not-configured')
        assert 'runs without these' in html

    def test_a_configured_tenant_shows_as_configured(self, admin_client, scannable):
        from mahj.tests.conftest import has_testid
        assert has_testid(self._page(admin_client), 'scanning-configured')

    def test_half_a_setup_names_the_missing_half(self, admin_client, keyed):
        """A key with no sheet leaves scanning off, and the reason is not
        guessable from the Scanning page's link text alone."""
        from mahj.tests.conftest import has_testid
        html = self._page(admin_client)
        assert has_testid(html, 'scanning-partial')
        assert 'score sheet is missing' in html

    def test_the_other_half_is_named_too(self, admin_client, tournament):
        from mahj.tests.conftest import has_testid
        ScanConfig.objects.create(tenant=tournament['tenant'], **SHEET)
        html = self._page(admin_client)
        assert has_testid(html, 'scanning-partial')
        assert 'API key is missing' in html

    def test_scanning_is_absent_where_the_build_has_none(self, admin_client, settings):
        from mahj.tests.conftest import has_testid
        settings.SCAN_ENABLED = False
        html = self._page(admin_client)
        assert not has_testid(html, 'scanning-not-configured')
        # The publish row is unaffected: standalone still publishes.
        assert has_testid(html, 'publish-not-configured')


class TestCrossTenantIsolation:

    def test_one_tenants_key_does_not_enable_another(self, tournament, keyed, tenant_b,
                                                     watched_queue):
        """Two tournaments on one host: the keyed one scans, the other cannot."""
        assert scan_key.resolve_key('test') == 'sk-ant-test-key-9fA2'
        assert scan_key.resolve_key('other') == ''
        assert client_for(HOST_B).post('/scan', {'image': _photo()}).status_code == 503
        assert watched_queue['enqueued'] == []


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

class TestResolution:

    def test_a_key_round_trips(self, tournament, keyed):
        assert scan_key.resolve_key('test') == 'sk-ant-test-key-9fA2'

    def test_no_row_is_no_key(self, tournament):
        assert scan_key.resolve_key('test') == ''

    def test_no_subdomain_is_no_key(self, db):
        assert scan_key.resolve_key('') == ''
        assert scan_key.is_configured('') is False

    def test_a_key_that_no_longer_decrypts_is_blank_not_an_exception(self, tournament,
                                                                     keyed, settings):
        """A DJANGO_SECRET_KEY rotation must disable scanning, not crash a worker
        on every job it picks up for the rest of the event."""
        settings.SECRET_KEY = 'a-completely-different-secret'
        assert scan_key.resolve_key('test') == ''
        assert scan_key.is_configured('test') is False

    @pytest.mark.parametrize('key,sheet', [(True, True), (True, False),
                                           (False, True), (False, False)])
    def test_availability_always_agrees_with_the_setup(self, tournament, key, sheet):
        """The accept-then-fail guard: two predicates that can disagree are two
        predicates that eventually will, and every disagreement is a player who
        waited on a spinner for a scan that was never going to happen."""
        fields = dict(SHEET) if sheet else {}
        if key:
            fields['api_key_enc'] = scan_key.encrypt('sk-ant-x')
        if fields:
            ScanConfig.objects.create(tenant=tournament['tenant'], **fields)
        setup = scan_key.resolve_setup('test')
        assert scan_key.is_configured('test') == setup.can_scan
        assert setup.can_scan == (key and sheet)

    def test_an_empty_row_is_not_configured(self, tournament):
        """A row created by one half of the page, with nothing filled in yet."""
        ScanConfig.objects.create(tenant=tournament['tenant'])
        assert scan_key.is_configured('test') is False


class TestAvailabilityCache:
    """Cached, but never *failing open*: a cache that can't answer must send us to
    the database, not hand out a licence to spend."""

    def test_it_is_cached_and_busted_by_a_save(self, tournament, settings,
                                               django_assert_num_queries):
        settings.CACHES = {
            'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
        from django.core.cache import cache
        cache.clear()

        cfg = ScanConfig.objects.create(tenant=tournament['tenant'],
                                        api_key_enc=scan_key.encrypt('sk-ant-x'),
                                        **SHEET)
        with django_assert_num_queries(1):
            assert scan_key.is_configured('test') is True
        with django_assert_num_queries(0):
            assert scan_key.is_configured('test') is True

        # Clearing the key takes effect now, not in five minutes.
        cfg.api_key_enc = None
        cfg.save()
        assert scan_key.is_configured('test') is False

    def test_a_missing_cache_reads_the_database_rather_than_failing_open(self, tournament):
        """The suite's DummyCache never returns anything — the opposite of the
        upload limiter, which fails open on purpose. Getting this backwards would
        mean an unkeyed tenant scanning during a Redis blip."""
        assert scan_key.is_configured('test') is False
        ScanConfig.objects.create(tenant=tournament['tenant'],
                                  api_key_enc=scan_key.encrypt('sk-ant-x'), **SHEET)
        assert scan_key.is_configured('test') is True

    def test_an_exploding_cache_does_not_take_the_page_down(self, tournament, scannable,
                                                            monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError('redis is on fire')
        monkeypatch.setattr('mahj.scan_key.cache.get', boom)
        monkeypatch.setattr('mahj.scan_key.cache.set', boom)
        assert scan_key.is_configured('test') is True


# ---------------------------------------------------------------------------
# No ambient credential, ever
# ---------------------------------------------------------------------------

class TestNoAmbientFallback:
    """The single most dangerous line this feature could grow is an
    `anthropic.Anthropic()` with no api_key=: the SDK would silently pick up the
    host's own credential and bill it, with nothing in any log to notice."""

    @pytest.fixture
    def fake_anthropic(self, monkeypatch):
        constructed = []
        module = types.ModuleType('anthropic')

        class Anthropic:
            def __init__(self, **kwargs):
                constructed.append(kwargs)
                raise AssertionError('the API client must not be constructed here')

        module.Anthropic = Anthropic
        for name in ('AuthenticationError', 'PermissionDeniedError', 'RateLimitError',
                     'NotFoundError', 'APIStatusError', 'APIConnectionError',
                     'APITimeoutError'):
            setattr(module, name, type(name, (Exception,), {}))
        monkeypatch.setitem(sys.modules, 'anthropic', module)
        return constructed

    def test_an_env_var_cannot_revive_scanning(self, monkeypatch, fake_anthropic):
        from mahj.views import scan
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-the-hosts-own-key')
        result = scan.run_scan(object(), '', None)
        assert result['status'] == 'error'
        assert fake_anthropic == [], 'no client may be constructed without a tenant key'

    def test_the_source_never_builds_a_client_without_an_explicit_key(self):
        """Cheap, and it catches the one-line regression the tests above can't:
        a future call site that constructs the client somewhere else. Parsed, not
        grepped, so the prose warning about this in run_scan's docstring doesn't
        read as an offence."""
        import ast
        from pathlib import Path
        for path in Path('mahj').rglob('*.py'):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == 'Anthropic'):
                    names = {kw.arg for kw in node.keywords}
                    assert 'api_key' in names, (
                        f'{path}:{node.lineno} builds a client with no api_key=, so '
                        f'the SDK would fall back to an ambient credential')

    def test_nothing_reads_the_env_var_any_more(self):
        """The app must not have a way to reach a credential a tenant didn't
        supply. (Removing the var from the deployed .env is cutover step 4 — a
        security control, not tidying — but that file isn't in the repo.)"""
        import ast
        from pathlib import Path
        for root in ('mahj', 'apps'):
            for path in Path(root).rglob('*.py'):
                if path.name.startswith('test_'):
                    continue
                for node in ast.walk(ast.parse(path.read_text())):
                    if isinstance(node, ast.Constant) and node.value == 'ANTHROPIC_API_KEY':
                        raise AssertionError(f'{path}:{node.lineno} still names the env var')

    def test_the_example_env_no_longer_offers_one(self):
        assert 'ANTHROPIC_API_KEY' not in Path('.env.example').read_text()


# ---------------------------------------------------------------------------
# The admin page
# ---------------------------------------------------------------------------

class TestScanningAdminPage:

    @pytest.fixture
    def admin_client(self, tournament):
        user = role_user('boss', tournament['tenant'], admin=True)
        c = client_for()
        c.force_login(user)
        return reauth(c)

    def _page(self, client):
        resp = client.get('/admin?page=scanning')
        assert resp.status_code == 200
        return resp.context['page_content']

    def test_every_control_is_actually_in_the_dom(self, admin_client, keyed):
        """Parsed, not grepped.

        The whole page once shipped inside an unterminated HTML comment: the
        markup was present in the response — so every substring assertion, and a
        curl-and-grep smoke test, passed — while the browser showed the heading
        and nothing else. Only a parser can tell rendered markup from commented-
        out markup.
        """
        from html.parser import HTMLParser

        class Ids(HTMLParser):
            def __init__(self):
                super().__init__()
                self.ids = set()

            def handle_starttag(self, tag, attrs):
                got = dict(attrs).get('id')
                if got:
                    self.ids.add(got)

        parser = Ids()
        parser.feed(self._page(admin_client))
        for control in ('sc-key', 'sc-save-key', 'sc-test-key', 'sc-template',
                        'sc-canvas', 'sc-save-template', 'sc-photo',
                        'sc-run-preview', 'sc-run-scan', 'sc-preview', 'sc-scores'):
            assert control in parser.ids, f'#{control} is not in the rendered page'

    def test_it_says_scanning_is_optional(self, admin_client):
        """A page full of API keys and crop boxes reads like a required setup step.
        It is not one: manual entry is the default and nothing else depends on
        scanning, so the page has to say so before an organiser starts worrying
        about it."""
        content = self._page(admin_client)
        assert 'Scanning is optional' in content
        assert 'by hand' in content

    def test_the_page_never_carries_the_key(self, admin_client, keyed):
        html = self._page(admin_client)
        assert 'sk-ant-test-key-9fA2' not in html
        assert '9fA2' in html, 'the tail is what identifies the key to support'

    def test_it_says_when_a_stored_key_cannot_be_read(self, tournament, keyed, settings):
        """After a DJANGO_SECRET_KEY rotation the row still says "configured" while
        nothing can read it — the page has to say which, or the organiser has no
        way to know why every scan suddenly fails.

        The client is built *after* the rotation on purpose: session cookies are
        signed with the same secret, so a rotation logs everyone out too.
        """
        settings.SECRET_KEY = 'a-completely-different-secret'
        user = role_user('boss2', tournament['tenant'], admin=True)
        c = client_for()
        c.force_login(user)
        reauth(c)
        assert 'no longer be read' in self._page(c)

    def test_it_shows_the_last_failure(self, admin_client, keyed):
        scan_key.stamp_error('test', 'The API key was rejected — check or replace it.')
        assert 'was rejected' in self._page(admin_client)

    def test_saving_a_key_stores_ciphertext_and_a_tail(self, admin_client, tournament):
        resp = admin_client.post('/scan_key_save', {'api_key': 'sk-ant-abcd1234'})
        assert resp.status_code == 200
        cfg = ScanConfig.objects.get(tenant=tournament['tenant'])
        assert bytes(cfg.api_key_enc) != b'sk-ant-abcd1234'
        assert cfg.key_tail == '1234'
        assert scan_key.resolve_key('test') == 'sk-ant-abcd1234'

    def test_a_blank_field_keeps_the_stored_key(self, admin_client, keyed):
        admin_client.post('/scan_key_save', {'api_key': '   '})
        assert scan_key.resolve_key('test') == 'sk-ant-test-key-9fA2'

    def test_clearing_wipes_the_key_and_the_tail(self, admin_client, keyed, tournament):
        admin_client.post('/scan_key_save', {'clear_key': '1'})
        cfg = ScanConfig.objects.get(tenant=tournament['tenant'])
        assert cfg.api_key_enc is None and cfg.key_tail == ''
        assert scan_key.is_configured('test') is False

    def test_an_odd_looking_key_warns_but_saves(self, admin_client):
        body = admin_client.post('/scan_key_save', {'api_key': 'not-a-real-prefix'}).json()
        assert body['warning']
        assert scan_key.resolve_key('test') == 'not-a-real-prefix'

    def test_a_scorer_cannot_reach_any_of_it(self, tournament):
        c = client_for()
        c.force_login(role_user('sam', tournament['tenant'], scorer=True))
        assert c.post('/scan_key_save', {'api_key': 'sk-ant-x'}).status_code in (403, 302)
        assert scan_key.resolve_key('test') == ''

    def test_get_is_refused(self, admin_client):
        assert admin_client.get('/scan_key_save').status_code == 405


class TestTestKeyIsFree:
    """The button has to stay free to press, or an organiser won't press it — and
    then the first time the key is exercised is at the venue."""

    @pytest.fixture
    def admin_client(self, tournament):
        user = role_user('boss', tournament['tenant'], admin=True)
        c = client_for()
        c.force_login(user)
        return reauth(c)

    @pytest.fixture
    def fake_anthropic(self, monkeypatch):
        calls = []
        module = types.ModuleType('anthropic')

        class Models:
            def retrieve(self, model):
                calls.append(('retrieve', model))
                return {'id': model}

        class Messages:
            def create(self, **kwargs):
                raise AssertionError('Test key must never buy an inference call')

        class Anthropic:
            def __init__(self, **kwargs):
                self.models, self.messages = Models(), Messages()

        module.Anthropic = Anthropic
        for name in ('AuthenticationError', 'PermissionDeniedError', 'RateLimitError',
                     'NotFoundError', 'APIStatusError', 'APIConnectionError',
                     'APITimeoutError'):
            setattr(module, name, type(name, (Exception,), {}))
        monkeypatch.setitem(sys.modules, 'anthropic', module)
        return calls

    def test_it_checks_the_model_and_reads_nothing(self, admin_client, fake_anthropic):
        from mahj.views.scan import OCR_MODEL
        body = admin_client.post('/scan_key_test', {'api_key': 'sk-ant-x'}).json()
        assert body['status'] == 'ok'
        assert fake_anthropic == [('retrieve', OCR_MODEL)]

    def test_it_falls_back_to_the_stored_key(self, admin_client, keyed, fake_anthropic):
        assert admin_client.post('/scan_key_test', {}).json()['status'] == 'ok'

    def test_with_no_key_anywhere_it_asks_for_one(self, admin_client, fake_anthropic):
        resp = admin_client.post('/scan_key_test', {})
        assert resp.status_code == 400
        assert fake_anthropic == []

    def test_a_rejected_key_is_a_sentence_not_a_500(self, admin_client, monkeypatch):
        module = types.ModuleType('anthropic')

        class AuthenticationError(Exception):
            pass

        class Anthropic:
            def __init__(self, **kwargs):
                self.models = self

            def retrieve(self, model):
                raise AuthenticationError('401 invalid x-api-key')

        module.Anthropic = Anthropic
        module.AuthenticationError = AuthenticationError
        for name in ('PermissionDeniedError', 'RateLimitError', 'NotFoundError',
                     'APIStatusError', 'APIConnectionError', 'APITimeoutError'):
            setattr(module, name, type(name, (Exception,), {}))
        monkeypatch.setitem(sys.modules, 'anthropic', module)

        resp = admin_client.post('/scan_key_test', {'api_key': 'sk-ant-bad'})
        assert resp.status_code == 400
        # The organiser gets an instruction; the SDK's text stays in the log.
        assert 'x-api-key' not in resp.json()['error']

    def test_a_host_without_the_sdk_says_so(self, admin_client, monkeypatch):
        """The working dev venv has no `anthropic` — this must not be a 500."""
        monkeypatch.setitem(sys.modules, 'anthropic', None)
        resp = admin_client.post('/scan_key_test', {'api_key': 'sk-ant-x'})
        assert resp.status_code == 400
        assert 'scanning software' in resp.json()['error']


# ---------------------------------------------------------------------------
# Dumps, resets and the standalone build
# ---------------------------------------------------------------------------

class TestSecretsStayOutOfDumps:

    def test_a_dump_carries_neither_the_key_nor_its_ciphertext(self, tournament, keyed):
        from mahj.tenant_dump import dump_tenant
        import gzip
        import json
        body = gzip.decompress(dump_tenant(tournament['tenant']))
        assert b'sk-ant-test-key-9fA2' not in body
        assert bytes(keyed.api_key_enc) not in body
        # Not a substring search: the migration stamp is literally "0021_scanconfig".
        assert 'ScanConfig' not in json.loads(body)['models']

    def test_a_restore_leaves_the_target_tenants_key_alone(self, tournament, keyed,
                                                           tenant_b):
        """Restoring last night's dump must not cost a tenant its paid credential."""
        from mahj.tenant_dump import dump_tenant, parse_dump, restore_tenant
        other_cfg = ScanConfig.objects.create(
            tenant=tenant_b, api_key_enc=scan_key.encrypt('sk-ant-others'))
        restore_tenant(tenant_b, parse_dump(dump_tenant(tournament['tenant'])))
        other_cfg.refresh_from_db()
        assert scan_key.decrypt(other_cfg.api_key_enc) == 'sk-ant-others'

    def test_a_full_reset_does_take_the_secrets(self, tournament, keyed):
        """The reset page wipes the tournament *and* what it was configured with."""
        from mahj.tenant_dump import wipe_tenant
        wipe_tenant(tournament['tenant'], include_secrets=True)
        assert not ScanConfig.objects.filter(tenant=tournament['tenant']).exists()

    def test_a_restore_wipe_does_not(self, tournament, keyed):
        from mahj.tenant_dump import wipe_tenant
        wipe_tenant(tournament['tenant'])
        assert ScanConfig.objects.filter(tenant=tournament['tenant']).exists()


class TestStandaloneBuild:
    """SCAN_ENABLED=False is the whole-install kill switch: no queue, no OCR deps,
    and so no page to configure any of it."""

    def test_the_page_disappears(self, tournament, settings):
        settings.SCAN_ENABLED = False
        user = role_user('boss', tournament['tenant'], admin=True)
        c = client_for()
        c.force_login(user)
        reauth(c)
        assert c.get('/admin?page=scanning').context['page_content'] == 'None'

    def test_the_endpoints_are_gone(self, tournament, settings):
        settings.SCAN_ENABLED = False
        user = role_user('boss', tournament['tenant'], admin=True)
        c = client_for()
        c.force_login(user)
        reauth(c)
        assert c.post('/scan_key_save', {'api_key': 'sk-ant-x'}).status_code == 404
        assert c.post('/scan_template_save', {}).status_code == 404

    def test_scanning_itself_is_gone(self, client_, tournament, keyed, settings):
        settings.SCAN_ENABLED = False
        assert client_.get('/scan').status_code == 404


class TestSetScanKeyCommand:
    """Cutover, and the only way to give a dev tenant a key: strict BYOK means
    there is no env fallback in development either."""

    def test_it_reads_the_key_from_stdin(self, tournament, monkeypatch):
        import io
        from django.core.management import call_command
        monkeypatch.setattr('sys.stdin', io.StringIO('sk-ant-piped-in-1234\n'))
        call_command('set_scan_key', 'test')
        assert scan_key.resolve_key('test') == 'sk-ant-piped-in-1234'

    def test_it_can_clear_one(self, tournament, keyed):
        from django.core.management import call_command
        call_command('set_scan_key', 'test', clear=True)
        assert scan_key.resolve_key('test') == ''

    def test_an_unknown_tournament_is_an_error(self, db):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with pytest.raises(CommandError):
            call_command('set_scan_key', 'nope')
