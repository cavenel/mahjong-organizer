"""\"View as\": a tenant admin previews the console as one of the single-role
accounts they hand out. The swap happens in helpers.current_membership, keyed on
the session and on the *real* membership being admin-tier, so everything
downstream — nav, page gates, role flags — follows without per-view changes.
"""
import pytest
from django.contrib.auth.models import User

from mahj.tests.conftest import has_testid, role_user
from mahj.views.helpers import VIEW_AS_SESSION_KEY


@pytest.fixture
def staff(tournament):
    return role_user('boss', tournament['tenant'], admin=True)


@pytest.fixture
def scorer(tournament):
    return role_user('sc', tournament['tenant'], scorer=True)


def _shell(client_, page='welcome'):
    resp = client_.get(f'/admin?page={page}')
    assert resp.status_code == 200
    return resp


def test_admin_previews_as_scorer(client_, staff, tournament):
    client_.force_login(staff)
    html = _shell(client_).content.decode()
    assert has_testid(html, 'view-as-menu') and not has_testid(html, 'view-as-banner')

    resp = client_.post('/admin?view_as=scorer')
    assert resp.status_code == 302
    html = _shell(client_).content.decode()
    # Looks exactly like a scorer's console...
    assert has_testid(html, 'view-as-banner')
    assert not has_testid(html, 'workspace-switcher')
    assert 'page=scoring' in html and 'page=publisher_overview' not in html
    assert 'Viewing as Scorer' in html
    # ...including the page gates: a Setup page is the empty panel.
    assert _shell(client_, 'settings').context['page_content'] == 'None'
    # The menu stays available to get back out.
    assert has_testid(html, 'view-as-menu') and has_testid(html, 'view-as-off')


def test_back_to_admin_view(client_, staff, tournament):
    client_.force_login(staff)
    client_.post('/admin?view_as=display_op')
    assert not has_testid(_shell(client_).content.decode(), 'workspace-switcher')
    client_.post('/admin?view_as=off')
    html = _shell(client_).content.decode()
    assert has_testid(html, 'workspace-switcher')
    assert not has_testid(html, 'view-as-banner')
    assert _shell(client_, 'settings').context['page_content'] != 'None'


def test_a_non_admin_cannot_change_role_this_way(client_, scorer, tournament):
    client_.force_login(scorer)
    assert client_.post('/admin?view_as=publisher').status_code == 302
    assert VIEW_AS_SESSION_KEY not in client_.session
    html = _shell(client_).content.decode()
    assert 'page=publisher_overview' not in html
    assert not has_testid(html, 'view-as-menu')


def test_a_planted_session_key_grants_nothing(client_, scorer, tournament):
    """The swap keys on the real membership: a scorer with the key in their
    session stays a scorer (and, in particular, never becomes a publisher)."""
    client_.force_login(scorer)
    session = client_.session
    session[VIEW_AS_SESSION_KEY] = 'publisher'
    session.save()
    html = _shell(client_).content.decode()
    assert 'page=publisher_overview' not in html
    assert not has_testid(html, 'view-as-banner')


def test_view_as_is_post_only(client_, staff, tournament):
    client_.force_login(staff)
    assert client_.get('/admin?view_as=scorer').status_code == 405
    assert VIEW_AS_SESSION_KEY not in client_.session


def test_unknown_role_is_ignored(client_, staff, tournament):
    client_.force_login(staff)
    client_.post('/admin?view_as=admin')
    assert VIEW_AS_SESSION_KEY not in client_.session
    assert has_testid(_shell(client_).content.decode(), 'workspace-switcher')


def test_superuser_previewing_loses_superuser_entries(client_, tournament):
    su = User.objects.create_superuser('root', '', 'pw')
    client_.force_login(su)
    assert 'page=tenants' in _shell(client_, 'setup').content.decode()
    client_.post('/admin?view_as=scorer')
    html = _shell(client_).content.decode()
    assert 'page=tenants' not in html and 'Superuser' not in html
    assert _shell(client_, 'tenants').context['page_content'] == 'None'


def test_logout_clears_the_preview(client_, staff, tournament):
    client_.force_login(staff)
    client_.post('/admin?view_as=scorer')
    client_.post('/admin?logout=1')
    assert VIEW_AS_SESSION_KEY not in client_.session
