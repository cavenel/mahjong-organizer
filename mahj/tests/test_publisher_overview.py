"""Publisher overview page: per-round summary counts, access control, and that
its data block is only rendered for publishers/staff.

The fixture (see conftest.tournament) seeds 3 rounds: rounds 1 & 2 are complete
(all tables scored, every score sheet validated) and published; round 3 has
seats but no scores and is unpublished.
"""
import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from mahj.models import Seat, ScoreSheet
from mahj.views.admin_views import publisher_overview_rows

HOST = 'test.mahj.ovh'


@pytest.fixture
def client_():
    c = Client()
    c.defaults['HTTP_HOST'] = HOST
    return c


@pytest.fixture
def publisher_user(db):
    u = User.objects.create_user('pub', password='pw')
    group, _ = Group.objects.get_or_create(name='Publisher')
    u.groups.add(group)
    return u


@pytest.fixture
def scorer_user(db):
    u = User.objects.create_user('sco', password='pw')
    group, _ = Group.objects.get_or_create(name='Scorer')
    u.groups.add(group)
    return u


@pytest.fixture
def staff_user(db):
    return User.objects.create_user('boss', password='pw', is_staff=True)


class TestOverviewRows:
    def _rows(self, tournament):
        return {
            r['round_nb']: r
            for r in publisher_overview_rows(tournament['tenant'], tournament['variable'])
        }

    def test_one_row_per_round(self, tournament):
        rows = publisher_overview_rows(tournament['tenant'], tournament['variable'])
        assert [r['round_nb'] for r in rows] == [1, 2, 3]

    def test_complete_published_round(self, tournament):
        r1 = self._rows(tournament)[1]
        assert r1['tables_total'] == 4
        assert sorted(r1['scored_tables']) == [1, 2, 3, 4]
        assert sorted(r1['validated_tables']) == [1, 2, 3, 4]
        assert r1['inprogress_tables'] == []
        assert r1['published'] is True
        assert r1['complete'] is True

    def test_partial_unpublished_round(self, tournament):
        r3 = self._rows(tournament)[3]
        assert r3['tables_total'] == 4
        assert r3['scored_tables'] == []
        assert r3['validated_tables'] == []
        assert r3['published'] is False
        assert r3['complete'] is False

    def test_in_progress_excludes_validated(self, tournament):
        # Wipe the validation marker on round 1 / table 1 but keep its 1-16 hands:
        # the sheet is now "in progress", not validated.
        tenant = tournament['tenant']
        ScoreSheet.objects.filter(
            tenant=tenant, round_nb=1, table_nb=1).update(validated=False)
        r1 = self._rows(tournament)[1]
        assert 1 in r1['inprogress_tables']
        assert 1 not in r1['validated_tables']
        assert sorted(r1['validated_tables']) == [2, 3, 4]

    def test_scored_requires_all_four_positions(self, tournament):
        # Blank one seat's minipoints in round 1 / table 1 → no longer scored.
        tenant = tournament['tenant']
        seat = Seat.objects.filter(tenant=tenant, round_nb=1, table_nb=1).first()
        seat.minipoints = None
        seat.save()
        r1 = self._rows(tournament)[1]
        assert 1 not in r1['scored_tables']
        assert r1['complete'] is False


class TestOverviewAccess:
    def test_publisher_sees_table(self, client_, tournament, publisher_user):
        client_.force_login(publisher_user)
        resp = client_.get('/options?page=publisher_overview')
        assert resp.status_code == 200
        body = resp.content.decode()
        assert 'Publisher overview' in body
        assert 'overview-data' in body  # the JSON data block is rendered

    def test_staff_sees_table(self, client_, tournament, staff_user):
        client_.force_login(staff_user)
        resp = client_.get('/options?page=publisher_overview')
        assert resp.status_code == 200
        assert 'overview-data' in resp.content.decode()

    def test_scorer_cannot_see_table(self, client_, tournament, scorer_user):
        # A scorer can open the admin dashboard but the publisher-only page must
        # render nothing (no data block, no heading).
        client_.force_login(scorer_user)
        resp = client_.get('/options?page=publisher_overview')
        assert resp.status_code == 200
        body = resp.content.decode()
        assert 'overview-data' not in body
        assert 'Publisher overview' not in body

    def test_sidebar_link_shown_to_publisher(self, client_, tournament, publisher_user):
        client_.force_login(publisher_user)
        resp = client_.get('/options?page=scoring')
        assert 'page=publisher_overview' in resp.content.decode()

    def test_sidebar_link_hidden_from_scorer(self, client_, tournament, scorer_user):
        client_.force_login(scorer_user)
        resp = client_.get('/options?page=scoring')
        assert 'page=publisher_overview' not in resp.content.decode()
