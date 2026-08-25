"""Verification for the admin shell navigation (mahj/templates/mahj/admin.html).

The console is two workspaces: **Setup** (before play) and **Run** (during play).
Renders the real `options` view end-to-end (view + context + template) for each
role and asserts:
  * the shell returns 200 for every role,
  * a tenant admin sees the workspace switcher, and the right sidebar for each
    workspace,
  * a single-role account sees no switcher and only its own Run groups,
  * the in-progress banner appears in Setup only, and only once play has begun.

Guards against the sidebar's `{% if %}` visibility guards drifting out of sync
with the server-side page gates in admin_views.py.
"""
import re

import pytest

from mahj.models import PublishedRound, Seat, TournamentSettings
from mahj.tests.conftest import has_testid, role_user


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


def _get_shell(client_, user, page='welcome'):
    client_.force_login(user)
    resp = client_.get(f'/admin?page={page}')
    assert resp.status_code == 200
    return resp.content.decode()


SETUP_GROUPS = ('tournament', 'players', 'print', 'administration')
RUN_GROUPS = ('scoring', 'displays', 'results')
ALL_GROUPS = SETUP_GROUPS + RUN_GROUPS


def _groups(html):
    """Which nav groups the shell rendered, by their data-testid."""
    return {g for g in ALL_GROUPS if has_testid(html, f'nav-group-{g}')}


# ── Tenant admin: two workspaces ───────────────────────────────────────────────

def test_staff_setup_workspace(client_, staff, tournament):
    html = _get_shell(client_, staff, 'setup')
    assert has_testid(html, 'workspace-switcher')
    assert _groups(html) == set(SETUP_GROUPS)
    for key in ('settings', 'import_template', 'player_editor', 'seating',
                'print_materials', 'users', 'backup', 'publish_target'):
        assert f'page={key}' in html, key
    # Live draws are Setup's last step.
    assert 'admin_player_draw' in html and 'admin_team_draw' in html
    # Nothing from Run leaks into the Setup sidebar.
    assert 'page=scoring' not in html
    assert not has_testid(html, 'nav-print-scores')


def test_staff_run_workspace(client_, staff, tournament):
    html = _get_shell(client_, staff, 'welcome')
    assert has_testid(html, 'workspace-switcher')
    assert _groups(html) == set(RUN_GROUPS)
    for key in ('scoring', 'publisher_overview', 'display', 'ceremony'):
        assert f'page={key}' in html, key
    assert has_testid(html, 'nav-print-scores')
    assert 'EMA_report.xlsx' in html
    # Nothing from Setup leaks into the Run sidebar (the switcher link aside).
    assert 'page=settings' not in html and 'page=player_editor' not in html


def test_every_setup_page_draws_the_setup_sidebar(client_, staff, tournament):
    for key in ('settings', 'import_template', 'player_editor', 'seating',
                'print_materials', 'publish_target'):
        html = _get_shell(client_, staff, key)
        assert _groups(html) == set(SETUP_GROUPS), key


def test_the_raw_database_admin_is_not_linked(client_, staff, tournament):
    """`/admin_db/` stays mounted (superuser only) but is a rescue tool, not a
    navigation item — in the standalone build the one local account is a
    superuser, which used to put the unscoped admin a click away."""
    from django.contrib.auth.models import User
    su = User.objects.create_superuser('root', '', 'pw')
    for page in ('setup', 'welcome'):
        html = _get_shell(client_, su, page)
        assert 'admin_db' not in html


def test_print_export_dropdown_is_gone(client_, staff, tournament):
    html = _get_shell(client_, staff, 'welcome')
    assert not has_testid(html, 'print-menu')
    assert 'Print / Export' not in html


# ── Single-role accounts: Run only, no switcher ───────────────────────────────

def test_scorer_sees_only_scoring(client_, scorer, tournament):
    html = _get_shell(client_, scorer)
    assert not has_testid(html, 'workspace-switcher')
    assert _groups(html) == {'scoring', 'results'}
    # Scorer is not a publisher: no publisher overview link.
    assert 'page=publisher_overview' not in html
    # Scorer can still reach the Scores print export...
    assert has_testid(html, 'nav-print-scores')
    # ...but nothing admin-only.
    assert 'EMA_report.xlsx' not in html
    assert 'page=print_materials' not in html and 'page=setup' not in html


def test_scorer_asking_for_a_setup_page_gets_the_run_sidebar(client_, scorer, tournament):
    """A forbidden page renders the empty panel; the shell around it must not
    switch to the Setup sidebar (which would list pages the scorer can't open)."""
    html = _get_shell(client_, scorer, 'settings')
    assert _groups(html) == {'scoring', 'results'}
    assert not has_testid(html, 'workspace-switcher')


def test_display_op_sees_only_displays(client_, display_op, tournament):
    html = _get_shell(client_, display_op)
    assert not has_testid(html, 'workspace-switcher')
    assert _groups(html) == {'displays'}
    assert 'page=display' in html and 'page=ceremony' in html
    # Display op has no print exports.
    assert not has_testid(html, 'nav-print-scores')


def test_publisher_sees_scoring_and_overview(client_, publisher, tournament):
    html = _get_shell(client_, publisher)
    assert not has_testid(html, 'workspace-switcher')
    assert _groups(html) == {'scoring', 'results'}
    assert 'page=publisher_overview' in html
    assert has_testid(html, 'nav-print-scores')


# ── Landing page and the in-progress banner ───────────────────────────────────
# The seeded `tournament` fixture is already under way (two rounds scored and
# published), so these start from a bare tenant: a chart with no scores yet.

@pytest.fixture
def fresh(tenant):
    TournamentSettings.objects.create(tenant=tenant, welcome='W', nb_rounds=3)
    Seat.objects.create(tenant=tenant, round_nb=1, table_nb=1, wind=1, draw_number=1)
    return {'tenant': tenant, 'staff': role_user('boss', tenant, admin=True)}


def test_admin_lands_in_setup_until_play_starts(client_, fresh):
    client_.force_login(fresh['staff'])
    assert client_.get('/admin').context['page'] == 'setup'
    PublishedRound.objects.create(tenant=fresh['tenant'], round_nb=1)
    assert client_.get('/admin').context['page'] == 'welcome'


def test_seeded_tournament_counts_as_in_progress(client_, staff, tournament):
    """Two rounds scored and published → the admin lands on the Run dashboard."""
    client_.force_login(staff)
    assert client_.get('/admin').context['page'] == 'welcome'


def test_in_progress_banner_only_in_setup_once_play_has_begun(client_, fresh):
    staff = fresh['staff']
    # Before play: no banner anywhere.
    assert not has_testid(_get_shell(client_, staff, 'setup'), 'setup-in-progress')
    # A published round means the tournament is under way.
    PublishedRound.objects.create(tenant=fresh['tenant'], round_nb=1)
    assert has_testid(_get_shell(client_, staff, 'setup'), 'setup-in-progress')
    assert has_testid(_get_shell(client_, staff, 'settings'), 'setup-in-progress')
    # Run pages never carry it.
    assert not has_testid(_get_shell(client_, staff, 'welcome'), 'setup-in-progress')
    assert not has_testid(_get_shell(client_, staff, 'scoring'), 'setup-in-progress')


def test_a_started_round_timer_counts_as_in_progress(client_, fresh):
    TournamentSettings.objects.filter(tenant=fresh['tenant']).update(counter=1_700_000_000_000)
    assert has_testid(_get_shell(client_, fresh['staff'], 'setup'), 'setup-in-progress')


# ── Live-sync socket wiring ───────────────────────────────────────────────────
#
# The Scoring grid and the Publisher overview sync across windows over the
# scorers WebSocket. The page opens it with connectScorerSocket({subdomain: ...}),
# and the helper *silently* skips the socket when the subdomain is empty — so a
# view that forgets to pass `subdomain` doesn't error, it just renders a page
# that never sees another window's edits (this happened once).

@pytest.mark.parametrize('page', ['scoring', 'publisher_overview'])
def test_live_sync_socket_gets_the_tenant_subdomain(client_, staff, tournament, page):
    html = _get_shell(client_, staff, page)
    subdomain = tournament['tenant'].subdomain
    assert subdomain
    assert 'connectScorerSocket(' in html
    # Scoring inlines the value; the overview binds it to a SUBDOMAIN const first.
    assert re.search(r'(?:subdomain: |SUBDOMAIN = )"%s"' % re.escape(subdomain), html)
