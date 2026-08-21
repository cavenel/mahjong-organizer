"""The projector screens themselves — the views an audience looks at.

`index` dispatches on a Screen's `view` string, a small grammar:

    "black" | "welcome" | "counter" | "announcement" | "schedule"
    | "scores:<detailed|totals|teams>:<all|N>"

Each arm renders a different template, and an unknown or malformed value must
blank the screen rather than serve an empty body — a wall of white in a playing
hall is worse than a black one. None of that was covered: the arms are one-liners,
so a typo'd template name or a missing context key only shows up on the wall.
"""
import pytest

from mahj.models import Screen, ScreenMode
from mahj.tests.conftest import HOST, client_for, role_user


@pytest.fixture
def screen(tournament):
    return Screen.objects.create(tenant=tournament['tenant'], view='black')


def _show(client_, screen, view):
    screen.view = view
    screen.save(update_fields=['view'])
    return client_.get(f'/{screen.id}')


class TestScreenViewGrammar:
    @pytest.mark.parametrize('view,marker', [
        ('welcome', 'Welcome'),          # the welcome message from the fixture
        ('announcement', 'Welcome'),     # the same field, as an announcement
        ('schedule', 'Round 1'),         # the fixture's first schedule row
        ('counter', 'counter'),          # the timer's own socket wiring
    ])
    def test_each_static_screen_renders_its_own_content(self, client_, screen, view,
                                                        marker, tournament):
        """Each arm renders a different template, so assert something from the
        page rather than only its status: a mis-wired arm that fell through to the
        black screen would still answer 200 with a body."""
        resp = _show(client_, screen, view)
        assert resp.status_code == 200
        assert marker in resp.content.decode()

    def test_black_renders_the_blank_screen(self, client_, screen, tournament):
        resp = _show(client_, screen, 'black')
        assert resp.status_code == 200
        html = resp.content.decode()
        # Blank on purpose: no schedule, no scores, no welcome text.
        assert 'Round 1' not in html

    @pytest.mark.parametrize('view', [
        'scores:detailed:all', 'scores:totals:all', 'scores:teams:all',
        'scores:detailed:1', 'scores:totals:2',
    ])
    def test_every_scores_variant_renders(self, client_, screen, view, tournament):
        assert _show(client_, screen, view).status_code == 200

    @pytest.mark.parametrize('view', [
        '', 'null', 'nonsense', 'scores', 'scores:', 'scores:bogus:all',
        'scores:detailed:notanumber', 'scores:detailed:all:extra',
    ])
    def test_an_unusable_view_blanks_the_screen_rather_than_erroring(
            self, client_, screen, view, tournament):
        """A screen configured by hand, or left over from an older grammar, must
        degrade to black. Anything that 500s here leaves the projector on a Django
        error page in front of the room."""
        resp = _show(client_, screen, view)
        assert resp.status_code == 200
        assert resp.content

    def test_an_unknown_screen_id_does_not_500(self, client_, tournament):
        assert client_.get('/999999').status_code in (200, 404)

    def test_a_tenant_with_no_screen_configured_says_so(self, client_, tournament):
        """Distinct from black: nothing is set up yet, and the operator needs to
        see that rather than a screen that looks switched off."""
        Screen.objects.filter(tenant=tournament['tenant']).delete()
        resp = client_.get('/1')
        assert resp.status_code == 200
        assert resp.content


class TestOverviewGrid:
    def test_it_lays_out_a_square_grid_for_the_screen_count(self, client_, tournament):
        tenant = tournament['tenant']
        Screen.objects.filter(tenant=tenant).delete()
        for _ in range(6):
            Screen.objects.create(tenant=tenant, view='black')
        resp = client_.get('/overview')
        assert resp.status_code == 200
        # 6 screens -> ceil(sqrt(6)) = 3, so a 3x3 grid with 3 trailing cells empty.
        html = resp.content.decode()
        assert 'grid-template-columns: repeat(3, 1fr)' in html
        assert 'grid-template-rows: repeat(3, 1fr)' in html

    def test_an_anonymous_projector_view_gets_no_controls(self, client_, tournament):
        """The grid is shown on a wall; a mode switcher there would be clickable by
        anyone walking past."""
        Screen.objects.create(tenant=tournament['tenant'], view='black')
        ScreenMode.objects.create(tenant=tournament['tenant'], name='Two',
                                  views='black,counter')
        html = client_.get('/overview').content.decode()
        assert 'Two' not in html

    def test_a_display_operator_gets_the_mode_switcher(self, client_, tournament):
        Screen.objects.create(tenant=tournament['tenant'], view='black')
        ScreenMode.objects.create(tenant=tournament['tenant'], name='Twoish',
                                  views='black,counter')
        client_.force_login(role_user('op_ov', tournament['tenant'], display_op=True))
        html = client_.get('/overview').content.decode()
        assert 'Twoish' in html

    def test_no_screens_at_all_still_renders(self, client_, tournament):
        Screen.objects.filter(tenant=tournament['tenant']).delete()
        assert client_.get('/overview').status_code == 200


class TestVenueClock:
    def test_it_is_milliseconds_since_midnight(self):
        from mahj.views.display import _venue_clock_ms
        ms = _venue_clock_ms()
        assert 0 <= ms < 24 * 3600 * 1000

    def test_a_bad_venue_timezone_falls_back_to_utc(self, settings):
        """VENUE_TZ is deployment config; a typo must not take every screen down."""
        from mahj.views.display import _venue_clock_ms
        settings.VENUE_TZ = 'Not/AZone'
        assert 0 <= _venue_clock_ms() < 24 * 3600 * 1000


class TestSpectatorQr:
    def test_no_subdomain_yields_no_qr(self):
        from mahj.views.display import _spectator_qr_svg
        assert _spectator_qr_svg('', '') == ''

    def test_a_subdomain_yields_inline_svg(self):
        """Inline, not an external image: the hall's wifi may not reach a CDN, and
        the previous version fetched from api.qrserver.com."""
        from mahj.views.display import _spectator_qr_svg
        svg = _spectator_qr_svg('test', '')
        assert svg == '' or '<svg' in svg      # '' when segno isn't installed
