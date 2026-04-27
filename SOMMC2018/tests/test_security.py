"""Authentication and CSRF protections on scorer/staff endpoints.

Covers:
- Staff-only views (options, admin_print_EMA, randomize, update_screen_view)
  redirect anonymous users to login.
- Scorer-gated view (admin_scores_per_hand) rejects anonymous/non-staff and
  allows users in the 'Scorer' group.
- CSRF is enforced on the POST endpoint update_hand_points.
"""
import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from SOMMC2018.models import Hand


HOST = 'test.mahj.ovh'


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


class TestStaffOnlyEndpointsRedirectAnonymous:
    """Anonymous users hitting staff-only URLs get a 302 to the login page."""

    @pytest.mark.parametrize('url', [
        '/options',
        '/EMA_report.xlsx',
        '/randomize',
        '/update_screen_view',
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
            'id': hand.id, 'version': hand.version, 'pts': 1, 'by': 1, 'from': 2,
        })
        assert resp.status_code == 302
        assert '/accounts/login/' in resp.url

    def test_update_position_points_anonymous_redirected(self, client_, tournament):
        resp = client_.get('/update_position_points', {'id': 1, 'mp': 10, 'tp': 4})
        assert resp.status_code == 302
        assert '/accounts/login/' in resp.url

    def test_non_scorer_redirected(self, client_, tournament, anonymous_user, hand):
        client_.force_login(anonymous_user)
        resp = client_.post('/update_hand_points', {
            'id': hand.id, 'version': hand.version, 'pts': 1, 'by': 1, 'from': 2,
        })
        assert resp.status_code == 302


class TestCsrfEnforcement:
    """POST endpoints must reject requests without a CSRF token."""

    def test_update_hand_points_rejects_without_csrf_token(self, csrf_client, hand, staff_user):
        csrf_client.force_login(staff_user)
        resp = csrf_client.post('/update_hand_points', {
            'id': hand.id, 'version': hand.version, 'pts': 10, 'by': 1, 'from': 2,
        })
        assert resp.status_code == 403

    def test_update_hand_points_accepts_with_csrf_token(self, csrf_client, hand, staff_user):
        csrf_client.force_login(staff_user)
        # Prime the CSRF cookie via any GET, then echo the token in the POST.
        csrf_client.get('/options')
        token = csrf_client.cookies['csrftoken'].value
        resp = csrf_client.post(
            '/update_hand_points',
            {'id': hand.id, 'version': hand.version, 'pts': 10, 'by': 1, 'from': 2},
            HTTP_X_CSRFTOKEN=token,
        )
        assert resp.status_code == 200
