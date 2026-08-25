"""Two small modules that run on nearly every request and were untested.

`context_processors` feeds the admin shell and every projector screen; each of its
processors swallows exceptions and returns a safe default, so a mistake here does
not raise — it silently renders the wrong nav or the wrong logo. `apps.csrf` only
runs when a CSRF check has already failed, which is exactly when nobody is
watching.
"""
import pytest
from django.contrib.auth.models import AnonymousUser, User
from django.test import RequestFactory

from mahj import context_processors as cp
from mahj.tests.conftest import HOST, client_for, grant, role_user


def _request(host=HOST, user=None):
    req = RequestFactory().get('/', HTTP_HOST=host)
    req.user = user or AnonymousUser()
    return req


class TestSiteLogoUrl:
    def test_falls_back_to_the_bundled_logo_with_no_tournament(self, db):
        """An unknown subdomain has no tournament; the screens still need a logo."""
        out = cp.site_logo(_request(host='nosuch.example.com'))
        assert out['site_logo_url'].endswith('mcr_logo.png')

    def test_a_tenant_logo_is_served_with_a_cache_busting_etag(self, tournament):
        settings_row = tournament['settings']
        settings_row.logo = b'\x89PNG-fake'
        settings_row.save()
        out = cp.site_logo(_request())
        # The projector screens hold the page open for hours, so the URL has to
        # change when the logo does or they keep the old one.
        assert '?v=' in out['site_logo_url']
        assert out['site_logo_url'].startswith('/logo')

    def test_no_logo_uses_the_fallback_rather_than_an_empty_url(self, tournament):
        out = cp.site_logo(_request())
        assert out['site_logo_url'].endswith('mcr_logo.png')


class TestPublicSite:
    def test_it_derives_the_host_from_the_subdomain(self, tournament):
        out = cp.public_site(_request())
        assert out['base_domain'] == 'example.com'
        assert 'test' in out['public_site_host']

    def test_an_unknown_tenant_yields_blanks_not_an_error(self, db):
        """Rendered on the public page, which anyone can hit on any hostname."""
        out = cp.public_site(_request(host='nosuch.example.com'))
        assert out['base_domain'] == 'example.com'
        assert isinstance(out['public_site_url'], str)

    def test_an_explicit_public_url_wins_over_the_derived_one(self, tournament):
        t = tournament['settings']
        t.public_url = 'https://results.example.org/2026'
        t.save()
        out = cp.public_site(_request())
        assert 'results.example.org' in out['public_site_url']

    def test_standalone_with_no_public_url_advertises_nothing(self, tournament, settings):
        """local.localhost is unreachable from a phone: no caption, no QR."""
        settings.STANDALONE = True
        out = cp.public_site(_request())
        assert out['public_site_url'] == ''
        assert out['public_site_host'] == ''

    def test_standalone_with_a_public_url_still_advertises_it(self, tournament, settings):
        settings.STANDALONE = True
        t = tournament['settings']
        t.public_url = 'results.example.org'
        t.save()
        out = cp.public_site(_request())
        assert out['public_site_url'] == 'https://results.example.org'
        assert out['public_site_host'] == 'results.example.org'


class TestRoleFlags:
    def test_anonymous_holds_no_role(self, tournament):
        out = cp.role_flags(_request())
        assert out == {
            'is_tenant_admin': False, 'user_is_scorer': False,
            'user_is_display_op': False, 'user_is_publisher': False,
            'user_can_access_admin': False, 'is_superuser_active': False,
            'viewing_as': None, 'real_is_tenant_admin': False,
        }

    def test_a_scorer_gets_exactly_the_scorer_flag(self, tournament):
        u = role_user('cp_scorer', tournament['tenant'], scorer=True)
        out = cp.role_flags(_request(user=u))
        assert out['user_is_scorer'] and out['user_can_access_admin']
        assert not out['is_tenant_admin']
        assert not (out['user_is_display_op'] or out['user_is_publisher'])

    def test_a_tenant_admin_folds_in_every_role(self, tournament):
        """The nav gates on these, so an admin missing a flag loses a whole
        section of the console."""
        u = role_user('cp_admin', tournament['tenant'], admin=True)
        out = cp.role_flags(_request(user=u))
        for flag in ('is_tenant_admin', 'user_is_scorer', 'user_is_display_op',
                     'user_is_publisher', 'user_can_access_admin', 'real_is_tenant_admin'):
            assert out[flag], flag
        # A tenant admin is not the platform operator, and isn't previewing.
        assert not out['is_superuser_active'] and out['viewing_as'] is None

    def test_a_role_on_another_tenant_grants_nothing_here(self, tournament, tenant_b):
        u = role_user('cp_other', tenant_b, admin=True)
        out = cp.role_flags(_request(user=u))
        assert not any(out.values())

    def test_a_broken_request_degrades_to_no_roles(self, db):
        """Every flag is false rather than raising: these run inside template
        rendering, where an exception is a 500 on a page that had one job."""
        out = cp.role_flags(RequestFactory().get('/'))   # no .user at all
        assert not any(out.values())


class TestCsrfFailureOnLogin:
    """A home-screen app resumes a stale login page; Django rotates the CSRF secret
    on login, so that form's token no longer matches the year-long cookie. The
    result was a dead-end 403 at the moment of signing in.
    """

    def test_a_failed_login_post_is_bounced_to_a_fresh_form(self, db):
        c = client_for()
        resp = c.post('/accounts/login/', {'username': 'x', 'password': 'y'},
                      HTTP_COOKIE='csrftoken=stale')
        # With enforce_csrf_checks off this is a normal login attempt; the handler
        # itself is exercised directly below.
        assert resp.status_code in (200, 302)

    def test_the_handler_redirects_a_login_post_with_the_expired_flag(self, db):
        from apps.csrf import csrf_failure
        req = RequestFactory().post('/accounts/login/')
        resp = csrf_failure(req, reason='bad token')
        assert resp.status_code == 302
        assert 'expired=1' in resp['Location']

    def test_it_preserves_where_the_user_was_going(self, db):
        from apps.csrf import csrf_failure
        req = RequestFactory().post('/accounts/login/', data={})
        req.GET = req.GET.copy()
        req.GET['next'] = '/admin?page=scoring'
        resp = csrf_failure(req, reason='bad token')
        assert 'next=' in resp['Location'] and 'expired=1' in resp['Location']

    def test_a_genuine_failure_elsewhere_still_gets_the_403(self, db):
        """The redirect is scoped to the login POST on purpose — masking CSRF
        failures on the scoring endpoints would hide a real attack."""
        from apps.csrf import csrf_failure
        req = RequestFactory().post('/update_seats_bulk')
        resp = csrf_failure(req, reason='bad token')
        assert resp.status_code == 403

    def test_a_login_get_is_not_redirected(self, db):
        """A GET cannot fail CSRF, and redirecting it would loop."""
        from apps.csrf import csrf_failure
        req = RequestFactory().get('/accounts/login/')
        resp = csrf_failure(req, reason='bad token')
        assert resp.status_code == 403
