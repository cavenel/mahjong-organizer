"""Authentication and CSRF protections on scorer/staff endpoints.

Covers:
- Staff-only views (options, admin_print_EMA, update_screen_view)
  redirect anonymous users to login.
- Scorer-gated view (admin_scores_per_hand) rejects anonymous/non-staff and
  allows users in the 'Scorer' group.
- CSRF is enforced on the POST endpoint update_hand_points.
"""
import json

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from mahj.models import Hand, TournamentSettings


HOST = 'test.example.com'


@pytest.fixture
def client_():
    c = Client()
    c.defaults['HTTP_HOST'] = HOST
    return c


@pytest.fixture
def csrf_client():
    c = Client(enforce_csrf_checks=True)
    c.defaults['HTTP_HOST'] = HOST
    return c


@pytest.fixture
def anonymous_user(db):
    return User.objects.create_user('regular', password='pw')


@pytest.fixture
def staff_user(db):
    return User.objects.create_user('staffer', password='pw', is_staff=True)


@pytest.fixture
def scorer_group_user(db):
    u = User.objects.create_user('scoreronly', password='pw')
    group, _ = Group.objects.get_or_create(name='Scorer')
    u.groups.add(group)
    return u


@pytest.fixture
def hand(tournament):
    return Hand.objects.filter(tenant=tournament['tenant'], round_nb=1, table_nb=1, hand_nb=1).first()


@pytest.fixture
def display_op_user(db):
    u = User.objects.create_user('displayop', password='pw')
    group, _ = Group.objects.get_or_create(name='Display_op')
    u.groups.add(group)
    return u


@pytest.fixture
def publisher_group_user(db):
    u = User.objects.create_user('publisheronly', password='pw')
    group, _ = Group.objects.get_or_create(name='Publisher')
    u.groups.add(group)
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

    def test_non_staff_redirected(self, client_, tournament, anonymous_user):
        client_.force_login(anonymous_user)
        resp = client_.get('/options')
        assert resp.status_code == 302
        assert '/accounts/login/' in resp.url

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

    def test_non_staff_non_scorer_redirected(self, client_, tournament, anonymous_user):
        client_.force_login(anonymous_user)
        resp = client_.get('/scores_per_hand_1_1')
        assert resp.status_code == 302

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

    def test_non_scorer_redirected(self, client_, tournament, anonymous_user, hand):
        client_.force_login(anonymous_user)
        resp = client_.post('/update_hand_points', {
            'id': hand.id, 'version': hand.version, 'points': 1, 'by': 1, 'from': 2,
        })
        assert resp.status_code == 302


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

    def test_scorer_group_member_redirected(self, client_, tournament, scorer_group_user):
        client_.force_login(scorer_group_user)
        resp = client_.post('/set_round_published',
                            data=json.dumps({'round_nb': 1, 'published': False}),
                            content_type='application/json')
        assert resp.status_code == 302
        assert '/accounts/login/' in resp.url

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

    The one hard rule is no overwrite — a table that already has hands is refused
    with a 409, for everyone, registered or not. Existing data can only be changed
    on the score sheet.
    """

    def test_scan_page_anonymous_ok(self, client_, tournament):
        assert client_.get('/scan').status_code == 200

    def test_scan_positions_anonymous_ok(self, client_, tournament):
        resp = client_.get('/scan_positions', {'round_nb': 1, 'table_nb': 1})
        assert resp.status_code == 200

    def test_scan_prefill_empty_table_anonymous_writes(self, client_, tournament):
        tenant = tournament['tenant']
        # Round 3 has positions but no hands seeded — an empty table, no conflict.
        body = {'round_nb': 3, 'table_nb': 1, 'validate': False,
                'scores': [{'Hand': 1, 'Value': 20, 'Winner': 1, 'Discarder': 2, 'Confidence': 0.5}]}
        resp = client_.post('/scan_prefill', data=json.dumps(body), content_type='application/json')
        assert resp.status_code == 200 and resp.json()['ok'] is True
        h = Hand.objects.get(tenant=tenant, round_nb=3, table_nb=1, hand_nb=1)
        assert h.points == 20 and h.win_by == 1 and h.win_from == 2

    def test_scan_prefill_filled_table_anonymous_conflicts(self, client_, tournament):
        tenant = tournament['tenant']
        # Round 1 table 1 is fully seeded — must never be overwritten by a scan.
        before = Hand.objects.get(tenant=tenant, round_nb=1, table_nb=1, hand_nb=1).points
        body = {'round_nb': 1, 'table_nb': 1, 'validate': False,
                'scores': [{'Hand': 1, 'Value': 999, 'Winner': 1, 'Discarder': 2, 'Confidence': 1.0}]}
        resp = client_.post('/scan_prefill', data=json.dumps(body), content_type='application/json')
        assert resp.status_code == 409 and resp.json()['conflict'] is True
        # Original data is untouched.
        after = Hand.objects.get(tenant=tenant, round_nb=1, table_nb=1, hand_nb=1).points
        assert after == before


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


class TestUserAdminGated:
    """The user-management endpoints are staff-only (not scorers)."""

    @pytest.mark.parametrize('url', USER_ADMIN_ENDPOINTS)
    def test_anonymous_redirected(self, client_, tournament, url):
        resp = _json_post(client_, '/' + url, {})
        assert resp.status_code == 302
        assert '/accounts/login/' in resp.url

    @pytest.mark.parametrize('url', USER_ADMIN_ENDPOINTS)
    def test_scorer_group_member_redirected(self, client_, tournament, scorer_group_user, url):
        client_.force_login(scorer_group_user)
        resp = _json_post(client_, '/' + url, {})
        assert resp.status_code == 302
        assert '/accounts/login/' in resp.url

    def test_users_page_hidden_from_scorer(self, client_, tournament, scorer_group_user):
        client_.force_login(scorer_group_user)
        resp = client_.get('/admin?page=users')
        # Non-staff reach the admin shell but the page body renders nothing.
        assert resp.status_code == 200
        assert b'User management' not in resp.content


class TestUserAdminActions:
    """Behaviour of the staff user-management endpoints."""

    def test_create_passwordless_user_with_roles(self, client_, tournament, staff_user):
        _login_reauthed(client_, staff_user)
        resp = _json_post(client_, '/user_create',
                          {'username': 'scorer1', 'roles': ['Scorer']})
        assert resp.status_code == 200
        u = User.objects.get(username='scorer1')
        assert not u.has_usable_password()        # blank password -> link-only
        assert u.groups.filter(name='Scorer').exists()
        assert not u.is_staff

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
        _json_post(client_, '/user_update_roles',
                   {'user_id': target.id, 'roles': ['Scorer', 'Publisher'], 'is_staff': False})
        assert set(target.groups.values_list('name', flat=True)) == {'Scorer', 'Publisher'}
        _json_post(client_, '/user_update_roles',
                   {'user_id': target.id, 'roles': ['Display_op'], 'is_staff': False})
        assert set(target.groups.values_list('name', flat=True)) == {'Display_op'}

    def test_cannot_remove_own_staff_flag(self, client_, tournament, staff_user):
        _login_reauthed(client_, staff_user)
        resp = _json_post(client_, '/user_update_roles',
                          {'user_id': staff_user.id, 'roles': [], 'is_staff': False})
        assert resp.status_code == 400
        staff_user.refresh_from_db()
        assert staff_user.is_staff is True

    def test_generate_link_authenticates(self, client_, tournament, staff_user):
        from sesame.utils import get_user
        _login_reauthed(client_, staff_user)
        target = User.objects.create_user('linkme')
        target.set_unusable_password(); target.save()
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
        token = get_token(target)
        assert get_user(token) == target            # valid before revoke
        resp = _json_post(client_, '/user_revoke_links', {'user_id': target.id})
        assert resp.status_code == 200
        target.refresh_from_db()
        assert get_user(token) is None              # old link no longer works

    def test_delete_user(self, client_, tournament, staff_user):
        _login_reauthed(client_, staff_user)
        target = User.objects.create_user('deleteme', password='pw')
        resp = _json_post(client_, '/user_delete', {'user_id': target.id})
        assert resp.status_code == 200
        assert not User.objects.filter(username='deleteme').exists()

    def test_cannot_delete_self(self, client_, tournament, staff_user):
        _login_reauthed(client_, staff_user)
        resp = _json_post(client_, '/user_delete', {'user_id': staff_user.id})
        assert resp.status_code == 400
        assert User.objects.filter(pk=staff_user.id).exists()

    def test_staff_can_delete_another_staff_when_more_than_one(self, client_, tournament, staff_user):
        _login_reauthed(client_, staff_user)
        other_staff = User.objects.create_user('staff2', password='pw', is_staff=True)
        resp = _json_post(client_, '/user_delete', {'user_id': other_staff.id})
        assert resp.status_code == 200
        assert not User.objects.filter(username='staff2').exists()


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

    def test_linkonly_staff_blocked(self, client_, tournament):
        # A staffer with no usable password has nothing to confirm, so user
        # management is closed to them entirely.
        u = User.objects.create_user('linkstaff', is_staff=True)
        u.set_unusable_password()
        u.save()
        client_.force_login(u)
        page = client_.get('/admin?page=users')
        assert b'Add a user' not in page.content
        assert b'unavailable' in page.content
        assert _json_post(client_, '/user_create', {'username': 'viad', 'roles': []}).status_code == 403

    def test_reauth_anonymous_redirected(self, client_, tournament):
        resp = _json_post(client_, '/user_reauth', {'password': 'pw'})
        assert resp.status_code == 302
        assert '/accounts/login/' in resp.url


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
