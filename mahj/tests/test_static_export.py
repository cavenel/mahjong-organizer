"""Static export of the public spectator page.

Exercises mahj.publish.static_export end-to-end against the seeded tournament:
the right files are produced, modal links are rewritten to resolvable .html
targets, the live WebSocket client is swapped for the poller, and the DB-served
tenant logo is emitted as a file. Reveal masking is inherited from the anonymous
views (round 3 is unpublished in the fixture) and asserted here too.
"""
import json

import pytest

from mahj.publish.static_export import export_public, _team_slug


@pytest.fixture
def teamed_tournament(tournament):
    """Give players two teams and the tenant a logo, to cover those export branches."""
    players = tournament['players']
    for i, p in enumerate(players):
        p.team = 'Alpha' if i % 2 == 0 else 'Beta'
        p.save()
    v = tournament['variable']
    v.logo = b'\x89PNG\r\n\x1a\n fake-png-bytes'
    v.logo_etag = 'deadbeef'
    v.save()
    return tournament


def _export(tmp_path, subdomain='test'):
    out = tmp_path / subdomain
    export_public(subdomain, out, copy_static=False)
    return out


class TestFilesProduced:
    def test_index_and_version(self, tmp_path, tournament):
        out = _export(tmp_path)
        assert (out / 'index.html').exists()
        version = json.loads((out / 'version.json').read_text())
        assert 'version' in version

    def test_player_and_score_modals(self, tmp_path, tournament):
        out = _export(tmp_path)
        pid = tournament['players'][0].id
        assert (out / f'details_player_{pid}.html').exists()
        # Round 1 table 1 exists in the fixture.
        assert (out / 'detailed_scores_1_1.html').exists()

    def test_team_modal_uses_slug(self, tmp_path, teamed_tournament):
        out = _export(tmp_path)
        assert (out / f'details_team_{_team_slug("Alpha")}.html').exists()
        assert (out / f'details_team_{_team_slug("Beta")}.html').exists()


class TestRewrites:
    def test_modal_links_get_html_suffix(self, tmp_path, tournament):
        out = _export(tmp_path)
        html = (out / 'index.html').read_text()
        pid = tournament['players'][0].id
        # A literal player link now points at the exported file.
        assert f'details_player_{pid}.html' in html
        # No bare (extension-less) modal href should survive.
        assert f'href="details_player_{pid}"' not in html

    def test_team_link_rewritten_to_slug(self, tmp_path, teamed_tournament):
        out = _export(tmp_path)
        html = (out / 'index.html').read_text()
        assert f'details_team_{_team_slug("Alpha")}.html' in html
        # The raw team name is gone from the href.
        assert 'href="details_team_Alpha"' not in html

    def test_static_urls_are_relative(self, tmp_path, tournament):
        # So the site can be hosted in a subfolder, not just at the domain root.
        html = (_export(tmp_path) / 'index.html').read_text()
        assert '"/static/' not in html          # no root-absolute static URLs
        assert 'src="static/js/static_poll.js"' in html

    def test_websocket_client_swapped_for_poller(self, tmp_path, tournament):
        out = _export(tmp_path)
        html = (out / 'index.html').read_text()
        assert 'display_socket' not in html
        assert 'static_poll.js' in html

    def test_logo_blob_written_and_referenced(self, tmp_path, teamed_tournament):
        out = _export(tmp_path)
        assert (out / 'logo.png').exists()
        html = (out / 'index.html').read_text()
        assert 'logo.png' in html
        assert '/logo?v=' not in html

    def test_no_logo_keeps_static_fallback(self, tmp_path, tournament):
        # Fixture tenant has no uploaded logo → the static mcr_logo stays, no file.
        out = _export(tmp_path)
        assert not (out / 'logo.png').exists()


class TestAuthMenu:
    def test_export_drops_login_menu(self, tmp_path, tournament):
        # No auth on a static host — the overflow menu and its login link are gone.
        html = (_export(tmp_path) / 'index.html').read_text()
        assert 'accounts/login' not in html
        assert 'open = !open' not in html   # the ⋮ overflow-menu toggle

    def test_export_drops_live_indicator(self, tmp_path, tournament):
        # No live socket on a static snapshot — the Live/Offline pill is gone.
        html = (_export(tmp_path) / 'index.html').read_text()
        assert 'x-show="wsConnected"' not in html

    def test_live_anon_view_keeps_login(self, tournament):
        # Regression guard: the normal (served) anon page still offers Login.
        from django.test import Client
        c = Client()
        c.defaults['HTTP_HOST'] = 'test.mahj.ovh'
        html = c.get('/').content.decode()
        assert 'accounts/login' in html


class TestPublishProgress:
    def test_status_requires_staff(self, tournament):
        from django.test import Client
        c = Client()
        c.defaults['HTTP_HOST'] = 'test.mahj.ovh'
        resp = c.get('/publish_status')
        assert resp.status_code in (301, 302)   # anonymous → login

    def test_status_idle_by_default(self, tournament):
        from django.contrib.auth.models import User
        from django.test import Client
        User.objects.create_user('boss', password='pw', is_staff=True)
        c = Client()
        c.defaults['HTTP_HOST'] = 'test.mahj.ovh'
        c.force_login(User.objects.get(username='boss'))
        assert c.get('/publish_status').json()['phase'] == 'idle'

    def test_progress_round_trip(self, settings):
        # The real cache (not the test DummyCache) is what the daemon thread and
        # the poll endpoint share; verify set/get under a working backend.
        settings.CACHES = {'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
        from django.core.cache import caches
        caches['default'].clear()
        from mahj.publish import trigger
        assert trigger.get_progress('t') is None
        trigger.set_progress('t', 'upload', pct=42, message='Uploading… 3/7')
        p = trigger.get_progress('t')
        assert p['phase'] == 'upload' and p['pct'] == 42


class TestRevealMasking:
    def test_unpublished_round_hidden(self, tmp_path, tournament):
        # Round 3 is unpublished in the fixture; the anonymous export must not
        # reveal its scores in the per-table modal.
        out = _export(tmp_path)
        html = (out / 'detailed_scores_3_1.html').read_text()
        # The "not revealed" placeholder stands in for the withheld grid.
        assert 'id="by_1"' not in html


class TestTriggerGating:
    """fire_static_export must stay a no-op unless SFTP is configured and the
    publishing tenant passes the PUBLISH_TENANT gate."""

    def test_unconfigured_is_noop(self, monkeypatch):
        from mahj.publish import trigger
        monkeypatch.delenv('PUBLISH_SFTP_HOST', raising=False)
        assert trigger._should_publish('test') is False

    def test_tenant_gate_blocks_mismatch(self, monkeypatch):
        from mahj.publish import trigger
        monkeypatch.setenv('PUBLISH_SFTP_HOST', 'host.example')
        monkeypatch.setenv('PUBLISH_TENANT', 'live')
        assert trigger._should_publish('test') is False
        assert trigger._should_publish('live') is True

    def test_configured_no_gate_allows_any(self, monkeypatch):
        from mahj.publish import trigger
        monkeypatch.setenv('PUBLISH_SFTP_HOST', 'host.example')
        monkeypatch.delenv('PUBLISH_TENANT', raising=False)
        assert trigger._should_publish('anything') is True


class TestPublishWebEndpoint:
    def test_requires_staff(self, tournament):
        from django.test import Client
        c = Client()
        c.defaults['HTTP_HOST'] = 'test.mahj.ovh'
        resp = c.post('/publish_web')
        # Anonymous → redirected to login, not a 200/OK publish.
        assert resp.status_code in (301, 302)

    def test_reports_when_unconfigured(self, tournament, monkeypatch):
        from django.contrib.auth.models import User
        from django.test import Client
        monkeypatch.delenv('PUBLISH_SFTP_HOST', raising=False)
        User.objects.create_user('boss', password='pw', is_staff=True)
        c = Client()
        c.defaults['HTTP_HOST'] = 'test.mahj.ovh'
        c.force_login(User.objects.get(username='boss'))
        resp = c.post('/publish_web')
        assert resp.status_code == 400
        assert 'not configured' in resp.json()['error'].lower()
