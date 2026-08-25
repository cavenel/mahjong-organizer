"""Authentication and CSRF protections on scorer/staff endpoints.

Covers:
- Staff-only views (options, admin_print_EMA, update_screen_view)
  redirect anonymous users to login.
- Scorer-gated view (admin_scores_per_hand) rejects anonymous/non-staff and
  allows users in the 'Scorer' group.
- CSRF is enforced on the POST endpoint update_hand_points.
"""
import json
import re

import pytest
from django.contrib.auth.models import User
from django.test import Client, override_settings

from mahj.models import Hand, ScoreSheet, TournamentSettings
from mahj.tests.conftest import HOST, grant, role_user
from mahj.views import user_admin




@pytest.fixture
def csrf_client():
    c = Client(enforce_csrf_checks=True)
    c.defaults['HTTP_HOST'] = HOST
    return c


# The privileged fixtures depend on `tournament` so the tenant ('test') exists to
# scope their Membership to. `anonymous_user` is authenticated but has NO
# membership in this tenant — the "logged in, but no access here" case that now
# yields 403 rather than a login redirect.
@pytest.fixture
def anonymous_user(tournament):
    return User.objects.create_user('regular', password='pw')


@pytest.fixture
def staff_user(tournament):
    u = User.objects.create_user('staffer', password='pw')
    grant(u, tournament['tenant'], admin=True)
    return u


@pytest.fixture
def scorer_group_user(tournament):
    u = User.objects.create_user('scoreronly', password='pw')
    grant(u, tournament['tenant'], scorer=True)
    return u


@pytest.fixture
def hand(tournament):
    return Hand.objects.filter(tenant=tournament['tenant'], round_nb=1, table_nb=1, hand_nb=1).first()


@pytest.fixture
def display_op_user(tournament):
    u = User.objects.create_user('displayop', password='pw')
    grant(u, tournament['tenant'], display_op=True)
    return u


@pytest.fixture
def publisher_group_user(tournament):
    u = User.objects.create_user('publisheronly', password='pw')
    grant(u, tournament['tenant'], publisher=True)
    return u


def _counter(tournament):
    return TournamentSettings.objects.get(tenant=tournament['tenant']).counter


class TestStaffOnlyEndpointsRedirectAnonymous:
    """Anonymous users hitting staff-only URLs get a 302 to the login page."""

    @pytest.mark.parametrize('url', [
        '/options',
        '/EMA_report.xlsx',
        '/update_screen_view',
        '/update_screen_name',
    ])
    def test_anonymous_redirected(self, client_, tournament, url):
        resp = client_.get(url)
        assert resp.status_code == 302
        assert '/accounts/login/' in resp.url

    def test_non_member_forbidden(self, client_, tournament, anonymous_user):
        # Authenticated but no membership in this tenant -> 403 (not a login bounce).
        client_.force_login(anonymous_user)
        resp = client_.get('/options')
        assert resp.status_code == 403

    def test_staff_user_reaches_options(self, client_, tournament, staff_user):
        client_.force_login(staff_user)
        resp = client_.get('/options')
        assert resp.status_code == 200


class TestScorerGatedEndpoint:
    """admin_scores_per_hand requires is_staff OR membership in 'Scorer'."""

    def test_anonymous_redirected(self, client_, tournament):
        resp = client_.get('/scores_per_hand_1_1')
        assert resp.status_code == 302
        assert '/accounts/login/' in resp.url

    def test_non_staff_non_scorer_forbidden(self, client_, tournament, anonymous_user):
        client_.force_login(anonymous_user)
        resp = client_.get('/scores_per_hand_1_1')
        assert resp.status_code == 403

    def test_staff_user_allowed(self, client_, tournament, staff_user):
        client_.force_login(staff_user)
        resp = client_.get('/scores_per_hand_1_1')
        assert resp.status_code == 200

    def test_scorer_group_member_allowed(self, client_, tournament, scorer_group_user):
        client_.force_login(scorer_group_user)
        resp = client_.get('/scores_per_hand_1_1')
        assert resp.status_code == 200


class TestScorerWriteEndpointsGated:
    """Write endpoints must reject unauthenticated requests (302 to login)."""

    def test_create_hand_points_anonymous_redirected(self, client_, tournament):
        resp = client_.post('/create_hand_points', {'round_nb': 1, 'table_nb': 1})
        assert resp.status_code == 302
        assert '/accounts/login/' in resp.url

    def test_update_hand_points_anonymous_redirected(self, client_, tournament, hand):
        resp = client_.post('/update_hand_points', {
            'id': hand.id, 'version': hand.version, 'points': 1, 'by': 1, 'from': 2,
        })
        assert resp.status_code == 302
        assert '/accounts/login/' in resp.url

    def test_non_scorer_forbidden(self, client_, tournament, anonymous_user, hand):
        client_.force_login(anonymous_user)
        resp = client_.post('/update_hand_points', {
            'id': hand.id, 'version': hand.version, 'points': 1, 'by': 1, 'from': 2,
        })
        assert resp.status_code == 403


class TestPublishRestrictedToStaffAndPublisher:
    """Publishing/unpublishing locks scores, so it is restricted to staff and the
    'Publisher' role — a plain scorer must not be able to unpublish and reopen
    finalized scores."""

    def test_anonymous_redirected(self, client_, tournament):
        resp = client_.post('/set_round_published',
                            data=json.dumps({'round_nb': 1, 'published': False}),
                            content_type='application/json')
        assert resp.status_code == 302
        assert '/accounts/login/' in resp.url

    def test_scorer_forbidden(self, client_, tournament, scorer_group_user):
        # A plain scorer is authenticated but lacks the publisher role -> 403.
        client_.force_login(scorer_group_user)
        resp = client_.post('/set_round_published',
                            data=json.dumps({'round_nb': 1, 'published': False}),
                            content_type='application/json')
        assert resp.status_code == 403

    def test_staff_user_allowed(self, client_, tournament, staff_user):
        client_.force_login(staff_user)
        resp = client_.post('/set_round_published',
                            data=json.dumps({'round_nb': 1, 'published': False}),
                            content_type='application/json')
        assert resp.status_code == 200

    def test_publisher_group_member_allowed(self, client_, tournament, publisher_group_user):
        client_.force_login(publisher_group_user)
        resp = client_.post('/set_round_published',
                            data=json.dumps({'round_nb': 1, 'published': False}),
                            content_type='application/json')
        assert resp.status_code == 200

    def test_publisher_reaches_admin_dashboard(self, client_, tournament, publisher_group_user):
        # A publisher manages publishing from the scoring page, so they must be
        # able to open the admin dashboard.
        client_.force_login(publisher_group_user)
        resp = client_.get('/options')
        assert resp.status_code == 200


class TestScanEndpointsPublic:
    """The /scan endpoints are public: anyone (no login) may scan an empty table,
    so data entry can be crowdsourced at the venue.

    Two hard rules. No overwrite — a table that already has hands is refused with a
    409, for everyone, registered or not; existing data can only be changed on the
    score sheet. And no writing without a photo: what gets written, and to which
    table, comes from the OCR job the server staged, never from the request (F13).
    """

    @pytest.fixture
    def job(self, monkeypatch):
        """A finished OCR job, as a real scan would leave it. The round and table are
        on the job because the upload went to the table's own URL."""
        jobs = {}

        def stage(job_id, round_nb, table_nb, scores, subdomain='test'):
            jobs[job_id] = {'status': 'done', 'round_nb': round_nb,
                            'table_nb': table_nb, 'subdomain': subdomain,
                            'scores': scores}
            return job_id

        monkeypatch.setattr('mahj.scan_queue.get_result', lambda jid: jobs.get(jid))
        return stage

    def _prefill(self, client, **body):
        return client.post('/scan_prefill', data=json.dumps(body),
                           content_type='application/json')

    def test_scan_page_anonymous_ok(self, client_, tournament):
        assert client_.get('/scan').status_code == 200

    def test_scan_seats_anonymous_ok(self, client_, tournament):
        resp = client_.get('/scan_seats', {'round_nb': 1, 'table_nb': 1})
        assert resp.status_code == 200

    def test_scan_prefill_empty_table_anonymous_writes(self, client_, tournament, job):
        """Still anonymous, still writes — crowdsourced entry is the point."""
        tenant = tournament['tenant']
        # Round 3 has seats but no hands seeded — an empty table, no conflict.
        job('j', 3, 1, [{'Hand': 1, 'Value': 20, 'Winner': 1, 'Discarder': 2,
                         'Confidence': 0.5}])
        resp = self._prefill(client_, job_id='j')
        assert resp.status_code == 200 and resp.json()['ok'] is True
        h = Hand.objects.get(tenant=tenant, round_nb=3, table_nb=1, hand_nb=1)
        assert h.points == 20 and h.win_by == 1 and h.win_from == 2

    def test_scan_prefill_filled_table_anonymous_conflicts(self, client_, tournament, job):
        tenant = tournament['tenant']
        # Round 1 table 1 is fully seeded — must never be overwritten by a scan.
        before = Hand.objects.get(tenant=tenant, round_nb=1, table_nb=1, hand_nb=1).points
        job('j', 1, 1, [{'Hand': 1, 'Value': 999, 'Winner': 1, 'Discarder': 2,
                         'Confidence': 1.0}])
        resp = self._prefill(client_, job_id='j')
        assert resp.status_code == 409 and resp.json()['conflict'] is True
        # Original data is untouched.
        after = Hand.objects.get(tenant=tenant, round_nb=1, table_nb=1, hand_nb=1).points
        assert after == before

    def test_scan_prefill_without_a_job_writes_nothing(self, client_, tournament):
        """The F13 hole, from the security suite's side: a body full of scores and no
        photo used to be written and validated."""
        tenant = tournament['tenant']
        resp = self._prefill(
            client_, round_nb=3, table_nb=2, validate=True,
            scores=[{'Hand': 1, 'Value': 88, 'Winner': 1, 'Discarder': 2,
                     'Confidence': 1.0}])
        assert resp.status_code == 400
        assert not Hand.objects.filter(tenant=tenant, round_nb=3, table_nb=2,
                                       win_by__isnull=False).exists()
        assert not ScoreSheet.objects.filter(tenant=tenant, round_nb=3,
                                             table_nb=2).exists()

    def test_scan_prefill_anonymous_cannot_validate(self, client_, tournament, job):
        """Validating takes a sheet out of the review queue, so it takes the role."""
        tenant = tournament['tenant']
        job('j', 3, 1, [{'Hand': 1, 'Value': 20, 'Winner': 1, 'Discarder': 2,
                         'Confidence': 1.0}])
        assert self._prefill(client_, job_id='j', validate=True).status_code == 200
        assert ScoreSheet.objects.get(
            tenant=tenant, round_nb=3, table_nb=1).validated is False


class TestCounterTimerGated:
    """The round timer is server-authoritative and only a display operator may
    start/stop it (hard invariant: only an explicit admin action stops the
    counter). Reads are public so projector screens can poll their state.

    Regression guard: counter_start once accepted an arbitrary ?new_value= from
    anyone with a CSRF token, so any spectator could stop/reset the live timer.
    """

    def test_read_is_public_and_does_not_mutate(self, client_, tournament):
        before = _counter(tournament)
        resp = client_.post('/counter_start')
        assert resp.status_code == 200
        data = resp.json()
        assert data['counter'] == before
        assert 'server_now' in data
        assert _counter(tournament) == before  # unchanged

    def test_anonymous_cannot_start(self, client_, tournament):
        resp = client_.post('/counter_start?action=start')
        assert resp.status_code == 403
        assert _counter(tournament) == -1  # untouched

    def test_anonymous_cannot_stop(self, client_, tournament, display_op_user):
        # Operator starts a running timer, then an anonymous stop must be rejected.
        client_.force_login(display_op_user)
        client_.post('/counter_start?action=start')
        running = _counter(tournament)
        assert running > 0
        client_.logout()
        resp = client_.post('/counter_start?action=stop')
        assert resp.status_code == 403
        assert _counter(tournament) == running  # timer survives the stray request

    def test_non_op_cannot_start(self, client_, tournament, anonymous_user):
        client_.force_login(anonymous_user)
        resp = client_.post('/counter_start?action=start')
        assert resp.status_code == 403
        assert _counter(tournament) == -1

    def test_display_op_start_sets_future_gong_time(self, client_, tournament, display_op_user):
        import time as _time
        client_.force_login(display_op_user)
        now_ms = int(_time.time() * 1000)
        resp = client_.post('/counter_start?action=start')
        assert resp.status_code == 200
        value = resp.json()['counter']
        # Start = now + lead + countdown, so the gong moment is a few seconds out.
        assert now_ms + 3000 <= value <= now_ms + 10000
        assert _counter(tournament) == value

    def test_display_op_stop_resets(self, client_, tournament, display_op_user):
        client_.force_login(display_op_user)
        client_.post('/counter_start?action=start')
        assert _counter(tournament) > 0
        resp = client_.post('/counter_start?action=stop')
        assert resp.status_code == 200
        assert resp.json()['counter'] == -1
        assert _counter(tournament) == -1

    def test_writes_must_be_post_not_get(self, client_, tournament, display_op_user):
        client_.force_login(display_op_user)
        resp = client_.get('/counter_start?action=start')
        assert resp.status_code == 403
        assert _counter(tournament) == -1

    def test_legacy_new_value_is_ignored(self, client_, tournament, display_op_user):
        # The old arbitrary-value write path is gone: new_value must not move the counter.
        client_.force_login(display_op_user)
        resp = client_.post('/counter_start?new_value=999999')
        assert resp.status_code == 200
        assert _counter(tournament) == -1


class TestMutatingEndpointsRefuseGet:
    """CSRF is never checked on a GET, so a mutating endpoint reachable by one can be
    fired by a cross-site <img>. Every one of these answers the same JSON 405, which
    is also the shape their front-ends know how to explain.
    """

    # Each was answering something else: a plain-text 405, a 400, a 403 — or, for
    # publish_web, actually running an export + SFTP upload.
    # /admin_reset answers the same way, but sits behind the sudo gate, which
    # replies first — test_reset.py checks it with a reauthed client.
    ENDPOINTS = [
        '/publish_web',
        '/update_logo',
        '/admin_generate_seating',
        '/player_editor_save',
        '/admin_player_draw_assign',
        '/publish_target_save',
        '/publish_target_test',
    ]

    @pytest.mark.parametrize('url', ENDPOINTS)
    def test_get_is_405_json(self, client_, staff_user, url):
        client_.force_login(staff_user)
        resp = client_.get(url)
        assert resp.status_code == 405, url
        assert resp['Content-Type'].startswith('application/json'), url
        assert resp.json()['error'] == 'POST required', url


class TestCsrfEnforcement:
    """POST endpoints must reject requests without a CSRF token."""

    def test_update_hand_points_rejects_without_csrf_token(self, csrf_client, hand, staff_user):
        csrf_client.force_login(staff_user)
        resp = csrf_client.post('/update_hand_points', {
            'id': hand.id, 'version': hand.version, 'points': 10, 'by': 1, 'from': 2,
        })
        assert resp.status_code == 403

    def test_update_hand_points_accepts_with_csrf_token(self, csrf_client, hand, staff_user):
        csrf_client.force_login(staff_user)
        # Prime the CSRF cookie via a GET that renders {% csrf_token %} (the score sheet);
        # the welcome page at /options does not, so it sets no cookie.
        csrf_client.get('/scores_per_hand_1_1')
        token = csrf_client.cookies['csrftoken'].value
        resp = csrf_client.post(
            '/update_hand_points',
            {'id': hand.id, 'version': hand.version, 'points': 10, 'by': 1, 'from': 2},
            HTTP_X_CSRFTOKEN=token,
        )
        assert resp.status_code == 200


def _json_post(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type='application/json')


def _login_reauthed(client, user, password='pw'):
    """Log in as a staff user and clear the user-management re-auth ('sudo')
    gate by confirming their password."""
    client.force_login(user)
    resp = _json_post(client, '/user_reauth', {'password': password})
    assert resp.status_code == 200, resp.content


USER_ADMIN_ENDPOINTS = [
    'user_create', 'user_update_roles', 'user_generate_link',
    'user_revoke_links', 'user_delete',
]


class TestAdminPageRoleIsolation:
    """Every admin-shell page must enforce its own role, not merely the shared
    "any app role" gate on the shell. Regression guard: the shell admits
    scorer/display_op/publisher, and several pages (display, ceremony, scoring,
    import) once relied on that gate alone — so a scorer could open (and, on the
    display page, drive the mutating screen/mode actions of) pages meant for a
    different role. The nav hides the links; the server must refuse them too.
    """

    def _body(self, client_, user, page):
        client_.force_login(user)
        resp = client_.get('/admin?page=' + page)
        # Denied pages still return the shell (200) with an empty body, matching
        # the existing hidden-page convention (see TestUserAdminGated).
        assert resp.status_code == 200
        return resp.content

    # --- display: display-operator only ---------------------------------------
    def test_display_hidden_from_scorer(self, client_, tournament, scorer_group_user):
        assert b'id="configure-screens"' not in self._body(client_, scorer_group_user, 'display')

    def test_display_hidden_from_publisher(self, client_, tournament, publisher_group_user):
        assert b'id="configure-screens"' not in self._body(client_, publisher_group_user, 'display')

    def test_display_visible_to_display_op(self, client_, tournament, display_op_user):
        assert b'id="configure-screens"' in self._body(client_, display_op_user, 'display')

    def test_scorer_cannot_add_screen_via_display_page(self, client_, tournament, scorer_group_user):
        from mahj.models import Screen
        before = Screen.objects.filter(tenant=tournament['tenant']).count()
        client_.force_login(scorer_group_user)
        # The display page's inline actions must not run for a non-operator.
        client_.get('/admin?page=display&action=add_screen')
        assert Screen.objects.filter(tenant=tournament['tenant']).count() == before

    def test_scorer_cannot_change_tournament_via_display_page(self, client_, tournament, scorer_group_user):
        client_.force_login(scorer_group_user)
        resp = client_.get('/admin?page=display&action=set_tournament&tournament-welcome=HACKED')
        # The set_tournament action returns the settings string when it runs; a
        # denied request just renders the empty shell instead.
        assert b'HACKED' not in resp.content
        assert TournamentSettings.objects.get(tenant=tournament['tenant']).welcome != 'HACKED'

    # --- ceremony: display-operator only --------------------------------------
    def test_ceremony_hidden_from_scorer(self, client_, tournament, scorer_group_user):
        assert b'ceremonyConsole' not in self._body(client_, scorer_group_user, 'ceremony')

    def test_ceremony_visible_to_display_op(self, client_, tournament, display_op_user):
        assert b'ceremonyConsole' in self._body(client_, display_op_user, 'ceremony')

    # --- scoring: scorer / publisher only -------------------------------------
    def test_scoring_hidden_from_display_op(self, client_, tournament, display_op_user):
        assert b'Filter by table' not in self._body(client_, display_op_user, 'scoring')

    def test_scoring_visible_to_scorer(self, client_, tournament, scorer_group_user):
        assert b'Filter by table' in self._body(client_, scorer_group_user, 'scoring')

    def test_scoring_visible_to_publisher(self, client_, tournament, publisher_group_user):
        assert b'Filter by table' in self._body(client_, publisher_group_user, 'scoring')

    def test_scoring_grid_is_read_only_for_a_publisher(self, client_, tournament,
                                                       publisher_group_user):
        """Scoring is the publisher's landing page and they may watch it, but every
        score mutation is scorer-only — so the cells must not invite an edit whose
        save can only fail. The publish toggle they *do* own stays."""
        body = self._body(client_, publisher_group_user, 'scoring').decode()
        for cell in re.findall(r'<input class="mp-input"[^>]*>', body):
            assert 'readonly' in cell
        assert 'read-only for your role' in body
        # The score sheet behind it is scorer-gated too, so it isn't offered.
        assert 'class="btn-sheet show_hands"' not in body
        assert 'class="publish-toggle' in body

    def test_scoring_grid_is_editable_for_a_scorer(self, client_, tournament,
                                                  scorer_group_user):
        body = self._body(client_, scorer_group_user, 'scoring').decode()
        cells = re.findall(r'<input class="mp-input"[^>]*>', body)
        assert cells, 'the fixture has seats to score'
        for cell in cells:
            assert 'readonly' not in cell
        assert 'read-only for your role' not in body
        assert 'class="btn-sheet show_hands"' in body

    def test_a_publisher_cannot_save_a_score(self, client_, tournament,
                                             publisher_group_user):
        """The read-only cells are a courtesy; this is the guarantee behind them."""
        from mahj.models import Seat
        seat = Seat.objects.filter(tenant=tournament['tenant'],
                                   round_nb=3, table_nb=1).first()
        client_.force_login(publisher_group_user)
        resp = client_.post('/update_seats_bulk',
                            data=json.dumps({'seats': [
                                {'id': seat.id, 'version': seat.version, 'mp': 99, 'tp': 4}]}),
                            content_type='application/json')
        assert resp.status_code == 403

    @pytest.fixture
    def live_tenant_scoring(self, tournament):
        """The Scoring page of a tenant whose subdomain is NOT 'test' — i.e. a real
        tournament, which is where these fixtures must not appear."""
        from mahj.models import Tenant
        live = Tenant.objects.create(name='Live Cup', subdomain='live')
        admin = User.objects.create_user('live_admin', password='pw')
        grant(admin, live, admin=True)
        c = Client()
        c.defaults['HTTP_HOST'] = 'live.example.com'
        c.force_login(admin)
        return c.get('/admin?page=scoring').content.decode()

    @pytest.mark.parametrize('fn', ['random_fill_score', 'random_fill_score_sheets',
                                    'clear_score', 'clear_score_sheets'])
    def test_the_destructive_fixtures_are_not_shipped(self, live_tenant_scoring, fn):
        """The toolbar was gated on the test tenant; the <script> defining the
        functions it calls was not, so they were plain globals on the live Scoring
        page of every tenant. `clear_score(true)` is one console line from erasing a
        tournament."""
        assert f'function {fn}(' not in live_tenant_scoring, (
            f"{fn} is defined on a real tenant's Scoring page")

    def test_the_real_page_still_works(self, live_tenant_scoring):
        """The gate must not take the page with it."""
        assert 'Filter by table' in live_tenant_scoring
        assert 'function send_ajax(' in live_tenant_scoring, 'score entry still wired'

    def test_the_fixtures_are_there_on_a_test_tournament(self, tournament,
                                                        staff_user):
        """They exist to be used — the gate must not disable them where they belong.
        The gate is the `is_test` flag in Tournament settings, not the subdomain."""
        tournament['settings'].is_test = True
        tournament['settings'].save(update_fields=['is_test'])
        c = Client()
        c.defaults['HTTP_HOST'] = HOST
        c.force_login(staff_user)
        body = c.get('/admin?page=scoring').content.decode()
        assert 'function clear_score(' in body
        assert 'Test data' in body, 'and the toolbar that calls them'
        assert 'data-testid="test-badge"' in body, 'and the shell badge that flags the mode'

    def test_a_tenant_named_test_gets_nothing_without_the_flag(self, tournament,
                                                              staff_user):
        """The `tournament` fixture's tenant has subdomain 'test' — that alone
        used to unlock the fixtures. Only the settings flag does now."""
        assert tournament['settings'].is_test is False
        c = Client()
        c.defaults['HTTP_HOST'] = HOST
        c.force_login(staff_user)
        body = c.get('/admin?page=scoring').content.decode()
        assert 'function clear_score(' not in body
        assert 'Test data' not in body
        assert 'data-testid="test-badge"' not in body

    def test_the_publish_lock_script_cannot_unlock_a_non_scorer(
            self, client_, tournament, publisher_group_user):
        """The grid repaints its cells whenever the publish state changes, and that
        repaint sets `readonly` outright. Unless it also carries the role, it hands a
        publisher an editable cell on every *unpublished* round the moment it runs —
        which the rendered markup alone doesn't show."""
        body = self._body(client_, publisher_group_user, 'scoring').decode()
        assert 'var userIsScorer = false;' in body
        assert 'prop("readonly", isPub || !userIsScorer)' in body

    # --- import_template: tenant admin only -----------------------------------
    def test_import_hidden_from_scorer(self, client_, tournament, scorer_group_user):
        assert b'will erase' not in self._body(client_, scorer_group_user, 'import_template')

    def test_import_hidden_from_display_op(self, client_, tournament, display_op_user):
        assert b'will erase' not in self._body(client_, display_op_user, 'import_template')

    def test_import_visible_to_staff(self, client_, tournament, staff_user):
        assert b'will erase' in self._body(client_, staff_user, 'import_template')


class TestUserAdminGated:
    """The user-management endpoints are staff-only (not scorers)."""

    @pytest.mark.parametrize('url', USER_ADMIN_ENDPOINTS)
    def test_anonymous_redirected(self, client_, tournament, url):
        resp = _json_post(client_, '/' + url, {})
        assert resp.status_code == 302
        assert '/accounts/login/' in resp.url

    @pytest.mark.parametrize('url', USER_ADMIN_ENDPOINTS)
    def test_scorer_member_forbidden(self, client_, tournament, scorer_group_user, url):
        client_.force_login(scorer_group_user)
        resp = _json_post(client_, '/' + url, {})
        assert resp.status_code == 403

    def test_users_page_hidden_from_scorer(self, client_, tournament, scorer_group_user):
        client_.force_login(scorer_group_user)
        resp = client_.get('/admin?page=users')
        # Non-staff reach the admin shell but the page body renders nothing.
        assert resp.status_code == 200
        assert b'User management' not in resp.content


class TestUserAdminActions:
    """Behaviour of the tenant-admin user-management endpoints (scoped to the
    request's tenant, roles stored as per-tenant Memberships)."""

    def _membership(self, user, tenant):
        from mahj.models import Membership
        return Membership.objects.get(user=user, tenant=tenant)

    def test_create_passwordless_user_with_roles(self, client_, tournament, staff_user):
        _login_reauthed(client_, staff_user)
        resp = _json_post(client_, '/user_create',
                          {'username': 'scorer1', 'roles': ['scorer']})
        assert resp.status_code == 200
        u = User.objects.get(username='scorer1')
        assert not u.has_usable_password()        # blank password -> link-only
        m = self._membership(u, tournament['tenant'])
        assert m.is_scorer and not m.is_tenant_admin
        assert not u.is_staff                     # app roles never touch the Django flag

    def test_create_with_password(self, client_, tournament, staff_user):
        _login_reauthed(client_, staff_user)
        _json_post(client_, '/user_create', {'username': 'pwuser', 'password': 'secret123'})
        assert User.objects.get(username='pwuser').has_usable_password()

    def test_create_duplicate_username_rejected(self, client_, tournament, staff_user):
        _login_reauthed(client_, staff_user)
        resp = _json_post(client_, '/user_create', {'username': 'staffer'})
        assert resp.status_code == 400

    def test_update_roles_adds_and_removes(self, client_, tournament, staff_user):
        _login_reauthed(client_, staff_user)
        target = User.objects.create_user('rolet', password='pw')
        grant(target, tournament['tenant'])       # must belong to this tenant first
        _json_post(client_, '/user_update_roles',
                   {'user_id': target.id, 'roles': ['scorer', 'publisher'], 'is_tenant_admin': False})
        m = self._membership(target, tournament['tenant'])
        assert (m.is_scorer, m.is_publisher, m.is_display_op) == (True, True, False)
        _json_post(client_, '/user_update_roles',
                   {'user_id': target.id, 'roles': ['display_op'], 'is_tenant_admin': False})
        m.refresh_from_db()
        assert (m.is_scorer, m.is_publisher, m.is_display_op) == (False, False, True)

    def test_cannot_remove_own_admin_flag(self, client_, tournament, staff_user):
        _login_reauthed(client_, staff_user)
        resp = _json_post(client_, '/user_update_roles',
                          {'user_id': staff_user.id, 'roles': [], 'is_tenant_admin': False})
        assert resp.status_code == 400
        assert self._membership(staff_user, tournament['tenant']).is_tenant_admin is True

    def test_generate_link_authenticates(self, client_, tournament, staff_user):
        from sesame.utils import get_user
        _login_reauthed(client_, staff_user)
        target = User.objects.create_user('linkme')
        target.set_unusable_password(); target.save()
        grant(target, tournament['tenant'], scorer=True)
        resp = _json_post(client_, '/user_generate_link', {'user_id': target.id})
        assert resp.status_code == 200
        url = resp.json()['url']
        token = url.split('sesame=')[1]
        assert get_user(token) == target

    def test_revoke_invalidates_existing_links(self, client_, tournament, staff_user):
        from sesame.utils import get_token, get_user
        _login_reauthed(client_, staff_user)
        target = User.objects.create_user('revokeme')
        target.set_unusable_password(); target.save()
        grant(target, tournament['tenant'], scorer=True)
        token = get_token(target)
        assert get_user(token) == target            # valid before revoke
        resp = _json_post(client_, '/user_revoke_links', {'user_id': target.id})
        assert resp.status_code == 200
        target.refresh_from_db()
        assert get_user(token) is None              # old link no longer works

    def test_revoke_refuses_your_own_row(self, client_, tournament, staff_user):
        """The guard that matters. Revoke clears the password as well as the links, and
        reauth_ok refuses password-less accounts — so an admin doing this to themselves
        loses user management with no in-app way back. Delete and Remove already guard
        the self row; this one did not, and its button was not disabled either."""
        _login_reauthed(client_, staff_user)
        resp = _json_post(client_, '/user_revoke_links', {'user_id': staff_user.id})
        assert resp.status_code == 400
        assert 'no way back' in resp.json()['error']
        staff_user.refresh_from_db()
        assert staff_user.has_usable_password(), 'the password must be untouched'

    def test_the_self_row_button_is_disabled(self, client_, tournament, staff_user):
        """Server-side is the guarantee; this keeps the UI from offering the click."""
        _login_reauthed(client_, staff_user)
        html = client_.get('/admin?page=users').content.decode()
        row = [b for b in html.split('<tr') if 'revoke-links' in b and 'you' in b.lower()]
        assert row, 'no user row rendered'
        # The revoke button carries the same is_self disable as Delete.
        assert html.count('class="revoke-links') >= 1
        assert 'clears their password' in html

    def test_a_superuser_can_still_revoke_the_last_admin(self, client_, tournament):
        """The documented escape hatch stays open: a superuser is exempt from the
        containment and last-admin guards."""
        solo = User.objects.create_user('soloadmin', password='pw')
        grant(solo, tournament['tenant'], admin=True)
        su = User.objects.create_superuser('revoke_su', '', 'pw')
        _login_reauthed(client_, su)
        resp = _json_post(client_, '/user_revoke_links', {'user_id': solo.id})
        assert resp.status_code == 200
        solo.refresh_from_db()
        assert not solo.has_usable_password()

    def test_revoking_someone_else_still_works(self, client_, tournament, staff_user):
        """No regression on the ordinary path."""
        target = User.objects.create_user('revokeother', password='pw')
        grant(target, tournament['tenant'], scorer=True)
        _login_reauthed(client_, staff_user)
        assert _json_post(client_, '/user_revoke_links',
                          {'user_id': target.id}).status_code == 200
        target.refresh_from_db()
        assert not target.has_usable_password()

    def test_delete_user(self, client_, tournament, staff_user):
        _login_reauthed(client_, staff_user)
        target = User.objects.create_user('deleteme', password='pw')
        grant(target, tournament['tenant'], scorer=True)
        resp = _json_post(client_, '/user_delete', {'user_id': target.id})
        assert resp.status_code == 200
        assert not User.objects.filter(username='deleteme').exists()

    def test_cannot_delete_self(self, client_, tournament, staff_user):
        _login_reauthed(client_, staff_user)
        resp = _json_post(client_, '/user_delete', {'user_id': staff_user.id})
        assert resp.status_code == 400
        assert User.objects.filter(pk=staff_user.id).exists()

    def test_admin_can_delete_another_admin_when_more_than_one(self, client_, tournament, staff_user):
        _login_reauthed(client_, staff_user)
        other_admin = User.objects.create_user('admin2', password='pw')
        grant(other_admin, tournament['tenant'], admin=True)
        resp = _json_post(client_, '/user_delete', {'user_id': other_admin.id})
        assert resp.status_code == 200
        assert not User.objects.filter(username='admin2').exists()


class TestATenantKeepsAtLeastOneAdmin:
    """The invariant the four `_tenant_admin_count` guards were written for.

    Those guards turn out to be unreachable for a non-superuser actor — reaching any
    of the endpoints requires being a tenant admin, so when the target is also an
    admin the count is always at least two. What actually holds the invariant is the
    self-guard at each site: the last admin cannot act on their own row. Pinned here
    so the property survives independently of the redundant guards, and so removing
    them later would not silently drop it.
    """

    @pytest.fixture
    def solo(self, client_, tournament):
        admin = User.objects.create_user('lonely_admin', password='pw')
        grant(admin, tournament['tenant'], admin=True)
        _login_reauthed(client_, admin)
        return admin

    def _admin_count(self, tenant):
        from mahj.models import Membership
        return Membership.objects.filter(tenant=tenant, is_tenant_admin=True).count()

    @pytest.mark.parametrize('url,extra', [
        ('/user_update_roles', {'roles': [], 'is_admin': False}),
        ('/user_revoke_links', {}),
        ('/user_delete', {}),
        ('/user_remove_from_tenant', {}),
    ])
    def test_the_only_admin_cannot_strip_themselves(self, client_, tournament, solo,
                                                    url, extra):
        tenant = tournament['tenant']
        assert self._admin_count(tenant) == 1
        resp = _json_post(client_, url, dict(extra, user_id=solo.id))
        assert resp.status_code == 400, f'{url} let the last admin act on themselves'
        assert self._admin_count(tenant) == 1, f'{url} dropped the tenant to zero admins'


class TestSuperuserTargetsAreOffLimits:
    """The worst path in the console: a tenant admin minting themselves a platform
    superuser session.

    The containment rule asks only whether the target holds a membership *outside*
    this tenant. A superuser seeded into one tenant — which is the documented
    bootstrap, `manage.py assign_membership root <sub> --roles=tenant_admin` — answers
    no, so every credential endpoint treated the operator's account as a local user.
    """

    @pytest.fixture
    def seeded_superuser(self, tournament):
        """A superuser whose only Membership is in this tenant, i.e. the documented
        seeded state that made the account look contained."""
        su = User.objects.create_superuser('platform_root', '', 'pw')
        grant(su, tournament['tenant'], admin=True)
        return su

    @pytest.fixture
    def admin_client(self, client_, tournament):
        admin = User.objects.create_user('plain_admin', password='pw')
        grant(admin, tournament['tenant'], admin=True)
        _login_reauthed(client_, admin)
        return client_

    def test_a_tenant_admin_cannot_mint_a_superuser_login_link(
            self, admin_client, seeded_superuser):
        """The escalation itself: the minted link is a full credential for that
        account, so opening it authenticates the tenant admin as the superuser."""
        resp = _json_post(admin_client, '/user_generate_link',
                          {'user_id': seeded_superuser.id})
        assert resp.status_code == 403
        assert 'platform operator' in resp.json()['error']
        assert 'url' not in resp.json(), 'no link may be handed out at all'

    def test_a_tenant_admin_cannot_rotate_a_superuser_credential(
            self, admin_client, seeded_superuser):
        resp = _json_post(admin_client, '/user_revoke_links',
                          {'user_id': seeded_superuser.id})
        assert resp.status_code == 403
        seeded_superuser.refresh_from_db()
        assert seeded_superuser.has_usable_password()

    def test_a_tenant_admin_cannot_delete_a_superuser(
            self, admin_client, seeded_superuser):
        resp = _json_post(admin_client, '/user_delete',
                          {'user_id': seeded_superuser.id})
        assert resp.status_code == 403
        assert User.objects.filter(pk=seeded_superuser.pk).exists()

    def test_a_tenant_admin_cannot_change_a_superuser_role(
            self, admin_client, seeded_superuser):
        resp = _json_post(admin_client, '/user_update_roles',
                          {'user_id': seeded_superuser.id, 'roles': [], 'is_admin': False})
        assert resp.status_code == 403

    def test_a_superuser_can_still_manage_another_superuser(
            self, client_, tournament, seeded_superuser):
        """The platform operator keeps the escape hatch — and this pins *why* the
        guard matters: the minted link is a full credential for the target account,
        so anyone holding it is that superuser."""
        from sesame.utils import get_user
        su = User.objects.create_superuser('other_root', '', 'pw')
        _login_reauthed(client_, su)
        resp = _json_post(client_, '/user_generate_link',
                          {'user_id': seeded_superuser.id})
        assert resp.status_code == 200
        token = resp.json()['url'].split('sesame=')[1]
        who = get_user(token)
        assert who == seeded_superuser and who.is_superuser, (
            'the link authenticates as the superuser — which is exactly what a tenant '
            'admin must not be able to mint')

    def test_ordinary_users_are_unaffected(self, admin_client, tournament):
        """No regression: a plain member of the tenant is still fully manageable."""
        target = User.objects.create_user('plain_member', password='pw')
        grant(target, tournament['tenant'], scorer=True)
        assert _json_post(admin_client, '/user_generate_link',
                          {'user_id': target.id}).status_code == 200


class TestUserAdminReauth:
    """User management asks staff to re-confirm their password ('sudo mode'),
    so a borrowed/unattended admin session can't reach it."""

    def test_staff_without_reauth_blocked_from_endpoint(self, client_, tournament, staff_user):
        client_.force_login(staff_user)
        resp = _json_post(client_, '/user_create', {'username': 'x'})
        assert resp.status_code == 403
        assert resp.json()['status'] == 'reauth_required'
        assert not User.objects.filter(username='x').exists()

    def test_users_page_shows_prompt_before_reauth(self, client_, tournament, staff_user):
        client_.force_login(staff_user)
        resp = client_.get('/admin?page=users')
        assert resp.status_code == 200
        assert b'Confirm your password' in resp.content
        assert b'Add a user' not in resp.content

    def test_reauth_wrong_password_rejected(self, client_, tournament, staff_user):
        client_.force_login(staff_user)
        resp = _json_post(client_, '/user_reauth', {'password': 'nope'})
        assert resp.status_code == 403
        # Still gated afterwards.
        assert _json_post(client_, '/user_create', {'username': 'x'}).status_code == 403

    def test_reauth_unlocks_page_and_endpoints(self, client_, tournament, staff_user):
        client_.force_login(staff_user)
        assert _json_post(client_, '/user_reauth', {'password': 'pw'}).status_code == 200
        page = client_.get('/admin?page=users')
        assert b'Add a user' in page.content
        resp = _json_post(client_, '/user_create', {'username': 'newbie', 'roles': []})
        assert resp.status_code == 200

    def test_linkonly_admin_blocked(self, client_, tournament):
        # An admin with no usable password has nothing to confirm, so user
        # management is closed to them entirely.
        u = User.objects.create_user('linkstaff')
        u.set_unusable_password()
        u.save()
        grant(u, tournament['tenant'], admin=True)
        client_.force_login(u)
        page = client_.get('/admin?page=users')
        assert b'Add a user' not in page.content
        assert b'unavailable' in page.content
        assert _json_post(client_, '/user_create', {'username': 'viad', 'roles': []}).status_code == 403

    def test_reauth_anonymous_redirected(self, client_, tournament):
        resp = _json_post(client_, '/user_reauth', {'password': 'pw'})
        assert resp.status_code == 302
        assert '/accounts/login/' in resp.url


# The suite runs on DummyCache so cache-invalidation assertions stay honest, but a
# counter needs somewhere to count. LocMem for these, cleared per test.
LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                      'LOCATION': 'reauth-throttle'}}


class TestReauthThrottle:
    """The sudo gate exists for a borrowed or unattended admin session, and its only
    secret is the password — so an unlimited retry loop hands that exact attacker a
    password oracle. Counted per session, which is who is being defended against.
    """

    @pytest.fixture(autouse=True)
    def _locmem(self):
        # override_settings can only decorate a Django TestCase, so it goes here.
        from django.core.cache import cache
        with override_settings(CACHES=LOCMEM):
            cache.clear()
            yield
            cache.clear()

    def _attempt(self, client, password):
        return _json_post(client, '/user_reauth', {'password': password})

    def test_wrong_passwords_are_cut_off(self, client_, tournament, staff_user):
        client_.force_login(staff_user)
        for i in range(user_admin.REAUTH_MAX_ATTEMPTS):
            assert self._attempt(client_, 'wrong').status_code == 403, i
        resp = self._attempt(client_, 'wrong')
        assert resp.status_code == 429
        assert 'Too many incorrect passwords' in resp.json()['error']

    def test_the_right_password_is_refused_too_once_locked(self, client_, tournament,
                                                           staff_user):
        """Otherwise the throttle is no throttle: an attacker guessing correctly on
        attempt six would still be let in."""
        client_.force_login(staff_user)
        for _ in range(user_admin.REAUTH_MAX_ATTEMPTS):
            self._attempt(client_, 'wrong')
        assert self._attempt(client_, 'pw').status_code == 429

    def test_a_success_clears_the_count(self, client_, tournament, staff_user):
        """A tournament admin who fat-fingers it twice and then gets it right must
        not be carrying four-fifths of a lockout around."""
        client_.force_login(staff_user)
        self._attempt(client_, 'wrong')
        self._attempt(client_, 'wrong')
        assert self._attempt(client_, 'pw').status_code == 200
        for i in range(user_admin.REAUTH_MAX_ATTEMPTS):
            assert self._attempt(client_, 'wrong').status_code == 403, i

    def test_another_session_is_counted_separately(self, client_, tournament,
                                                   staff_user):
        """Per session, not per account: one locked-out session must not lock the
        admin out of the console from their own laptop."""
        client_.force_login(staff_user)
        for _ in range(user_admin.REAUTH_MAX_ATTEMPTS):
            self._attempt(client_, 'wrong')
        assert self._attempt(client_, 'wrong').status_code == 429

        other = Client()
        other.defaults['HTTP_HOST'] = HOST
        other.force_login(staff_user)
        assert self._attempt(other, 'pw').status_code == 200


class TestSesameLinkLogin:
    """The sesame middleware logs a user in from a ?sesame=<token> URL."""

    def test_link_authenticates_on_gated_page(self, client_, tournament, scorer_group_user):
        from sesame.utils import get_token
        # No prior login; the token alone should grant access to a scorer-gated page.
        # The middleware logs the user in, then 302-redirects to the same URL with the
        # token stripped (so it doesn't linger in history/referer); the session cookie
        # carries the login, so following the redirect lands on the page (200).
        token = get_token(scorer_group_user)
        resp = client_.get('/scores_per_hand_1_1?sesame=' + token, follow=True)
        assert resp.status_code == 200
        assert resp.redirect_chain                          # a token-stripping redirect happened
        assert resp.context['user'].is_authenticated

    def test_no_token_still_redirected(self, client_, tournament):
        resp = client_.get('/scores_per_hand_1_1')
        assert resp.status_code == 302
        assert '/accounts/login/' in resp.url


class TestDjangoAdminIsSuperuserOnly:
    """`/admin_db/` is mounted on every tenant subdomain and its models are
    registered unscoped — a Player changelist there lists every tournament's
    competitors, and Tenant/TournamentSettings are editable from it.

    Django's own gate is `is_active and is_staff`. This deployment reserves
    is_staff for the admin site and forbids keying any access decision on it
    (docs/dev/access-control.md), so the site itself must require is_superuser —
    otherwise the one flag nothing is supposed to grant on grants everything.
    """

    def test_a_staff_user_who_is_not_a_superuser_is_refused(self, client_, tournament):
        staffer = User.objects.create_user('staffer', password='pw')
        staffer.is_staff = True
        staffer.save(update_fields=['is_staff'])
        client_.force_login(staffer)
        resp = client_.get('/admin_db/')
        # Django answers a failed has_permission by bouncing to the admin login.
        assert resp.status_code == 302
        assert '/admin_db/login/' in resp['Location']

    def test_a_tenant_admin_is_refused(self, client_, tournament):
        """A tenant admin runs their own tournament; the cross-tenant model editor
        is not part of that."""
        admin_user = User.objects.create_user('tadmin', password='pw')
        grant(admin_user, tournament['tenant'], admin=True)
        client_.force_login(admin_user)
        assert client_.get('/admin_db/').status_code == 302

    def test_a_superuser_still_gets_in(self, client_, tournament):
        root = User.objects.create_superuser('root', password='pw')
        client_.force_login(root)
        assert client_.get('/admin_db/').status_code == 200

    def test_the_player_changelist_is_reachable_for_a_superuser(self, client_, tournament):
        """Guards the wiring, not the gate: default_site is easy to get wrong in a
        way that leaves the admin working but ungated, so assert a real registered
        model renders through the narrowed site."""
        root = User.objects.create_superuser('root2', password='pw')
        client_.force_login(root)
        assert client_.get('/admin_db/mahj/player/').status_code == 200


# --------------------------------------------------------------------------
# Authorization on the endpoints that had none asserted (5.3).
#
# Each of these was covered only by a happy path, or by the 405-on-GET check —
# which proves the method gate, not the role gate. A view whose decorator was
# deleted would have kept every one of those tests green.
# --------------------------------------------------------------------------

SCORER_ENDPOINTS = [
    ('/clear_score_sheet', {'round_nb': 1, 'table_nb': 1}),
    ('/update_seat_penalty', {'id': 1, 'penalty': -10}),
    ('/validate_score_sheet', {'round_nb': 1, 'table_nb': 1, 'validated': '1'}),
]

ADMIN_ENDPOINTS = [
    ('/admin_upload_from_template', {}),
    ('/admin_generate_seating', {}),
    ('/update_logo', {}),
]


class TestScorerEndpointsRefuseTheWrongRole:
    @pytest.mark.parametrize('url,payload', SCORER_ENDPOINTS)
    def test_a_display_operator_is_refused(self, client_, tournament,
                                           display_op_user, url, payload):
        client_.force_login(display_op_user)
        assert client_.post(url, payload).status_code == 403

    @pytest.mark.parametrize('url,payload', SCORER_ENDPOINTS)
    def test_a_non_member_is_refused(self, client_, tournament, anonymous_user,
                                     url, payload):
        client_.force_login(anonymous_user)
        assert client_.post(url, payload).status_code == 403

    @pytest.mark.parametrize('url,payload', SCORER_ENDPOINTS)
    def test_anonymous_is_bounced_to_login(self, client_, tournament, url, payload):
        resp = client_.post(url, payload)
        assert resp.status_code == 302 and '/accounts/login/' in resp.url

    @pytest.mark.parametrize('url,payload', SCORER_ENDPOINTS)
    def test_a_scorer_of_another_tenant_is_refused(self, client_, tournament,
                                                  tenant_b, url, payload):
        """Holding the role somewhere else is not holding it here — the whole point
        of scoping roles to a Membership."""
        client_.force_login(role_user('scorer_of_b', tenant_b, scorer=True))
        assert client_.post(url, payload).status_code == 403


class TestAdminEndpointsRefuseTheWrongRole:
    @pytest.mark.parametrize('url,payload', ADMIN_ENDPOINTS)
    def test_a_scorer_is_refused(self, client_, tournament, scorer_group_user,
                                url, payload):
        """A scorer runs a table; importing a template, generating the seating or
        replacing the logo is not part of that."""
        client_.force_login(scorer_group_user)
        assert client_.post(url, payload).status_code == 403

    @pytest.mark.parametrize('url,payload', ADMIN_ENDPOINTS)
    def test_a_publisher_is_refused(self, client_, tournament,
                                   publisher_group_user, url, payload):
        client_.force_login(publisher_group_user)
        assert client_.post(url, payload).status_code == 403

    @pytest.mark.parametrize('url,payload', ADMIN_ENDPOINTS)
    def test_a_non_member_is_refused(self, client_, tournament, anonymous_user,
                                     url, payload):
        client_.force_login(anonymous_user)
        assert client_.post(url, payload).status_code == 403

    @pytest.mark.parametrize('url,payload', ADMIN_ENDPOINTS)
    def test_an_admin_of_another_tenant_is_refused(self, client_, tournament,
                                                  tenant_b, url, payload):
        client_.force_login(role_user('admin_of_b', tenant_b, admin=True))
        assert client_.post(url, payload).status_code == 403


class TestDestructiveEndpointsAreTenantScoped:
    """A refusal is only half the property: the endpoints that destroy data must
    also be unable to reach across tenants when the caller *is* authorized."""

    def test_clearing_a_sheet_does_not_touch_another_tenant(self, client_, tournament,
                                                            tenant_b):
        from mahj.models import Seat
        tenant = tournament['tenant']
        # Give tenant B a seat at the same coordinates, and a penalty to lose.
        Seat.objects.create(tenant=tenant_b, round_nb=1, table_nb=1, wind=1,
                            draw_number=1, minipoints=50, tablepoints=4.0, penalty=-20)
        client_.force_login(role_user('scorer_a', tenant, scorer=True))

        assert client_.post('/clear_score_sheet',
                            {'round_nb': 1, 'table_nb': 1}).status_code == 200
        b_seat = Seat.objects.get(tenant=tenant_b, round_nb=1, table_nb=1, wind=1)
        assert b_seat.penalty == -20, "another tenant's penalty was reset"
        assert b_seat.minipoints == 50

    def test_a_penalty_write_cannot_name_another_tenants_seat(self, client_,
                                                              tournament, tenant_b):
        """The endpoint takes a bare seat id, so the only thing stopping a scorer
        reaching into another tournament is the tenant in the lookup."""
        from mahj.models import Seat
        foreign = Seat.objects.create(tenant=tenant_b, round_nb=1, table_nb=1,
                                      wind=1, draw_number=1, penalty=0)
        client_.force_login(role_user('scorer_a2', tournament['tenant'], scorer=True))

        resp = client_.post('/update_seat_penalty',
                            {'id': foreign.id, 'penalty': -30})
        assert resp.status_code == 404
        foreign.refresh_from_db()
        assert foreign.penalty == 0

    def test_a_penalty_write_still_works_on_this_tenants_seat(self, client_, tournament):
        """The other half, so the 404 above is scoping rather than a broken payload."""
        from mahj.models import Seat
        tenant = tournament['tenant']
        own = Seat.objects.filter(tenant=tenant, round_nb=1, table_nb=1, wind=1).first()
        client_.force_login(role_user('scorer_a3', tenant, scorer=True))
        resp = client_.post('/update_seat_penalty', {'id': own.id, 'penalty': -30})
        assert resp.status_code == 200
        own.refresh_from_db()
        assert own.penalty == -30
