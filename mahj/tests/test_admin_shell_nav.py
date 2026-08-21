"""Verification for the redesigned admin shell navigation (mahj/templates/mahj/admin.html).

Renders the real `options` view end-to-end (view + context + template) for each
role and asserts:
  * the page returns 200 (shell renders without error), and
  * role-based section visibility matches each role (staff sees every section;
    scorer/display_op/publisher see only their own).

Guards against the sidebar's `{% if %}` visibility guards drifting out of sync
with the server-side page gates in admin_views.py.
"""
import pytest

from mahj.tests.conftest import grant, has_testid, role_user



@pytest.fixture
def staff(tournament):
    return role_user('boss', tournament['tenant'], admin=True)


@pytest.fixture
def scorer(tournament):
    return role_user('sc', tournament['tenant'], scorer=True)


@pytest.fixture
def display_op(tournament):
    return role_user('op', tournament['tenant'], display_op=True)


@pytest.fixture
def publisher(tournament):
    return role_user('pub', tournament['tenant'], publisher=True)


def _get_shell(client_, user):
    client_.force_login(user)
    resp = client_.get('/admin?page=welcome')
    assert resp.status_code == 200
    return resp.content.decode()


ALL_GROUPS = ('configuration', 'players', 'scoring', 'displays',
              'results', 'administration')


def _groups(html):
    """Which nav groups the shell rendered, by their data-testid."""
    return {g for g in ALL_GROUPS if has_testid(html, f'nav-group-{g}')}


def test_staff_sees_every_section(client_, staff, tournament):
    html = _get_shell(client_, staff)
    assert _groups(html) == set(ALL_GROUPS)
    # Print / Export lives in the topbar dropdown, not the nav.
    assert has_testid(html, 'print-menu')
    assert "showPrintModal('player_names')" in html


def test_scorer_sees_only_scoring(client_, scorer, tournament):
    html = _get_shell(client_, scorer)
    assert _groups(html) == {'scoring'}
    # Scorer is not a publisher: no publisher overview link.
    assert 'page=publisher_overview' not in html
    # Scorer can still reach the Scores print export.
    assert "showPrintModal('print_scores')" in html
    # ...but not the staff-only prepare exports.
    assert "showPrintModal('player_names')" not in html


def test_display_op_sees_only_displays(client_, display_op, tournament):
    html = _get_shell(client_, display_op)
    assert _groups(html) == {'displays'}
    assert 'page=display' in html and 'page=ceremony' in html
    # Display op has no print exports → no print/export dropdown at all.
    assert not has_testid(html, 'print-menu')


def test_publisher_sees_scoring_and_overview(client_, publisher, tournament):
    html = _get_shell(client_, publisher)
    # The publisher overview link lives inside the Scoring group, so a publisher
    # sees that one group and no Results group of their own.
    assert _groups(html) == {'scoring'}
    assert 'page=publisher_overview' in html

