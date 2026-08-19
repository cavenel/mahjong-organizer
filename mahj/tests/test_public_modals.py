"""Public desktop modal endpoints (opened by the spectator crowd).

detailed_scores is unauthenticated and linked from every table cell on the
desktop, including unplayed rounds. Guard the two properties that matter under
crowd load: it must not write rows on a read, and it must not N+1 on players.
"""
import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client

from mahj.models import Hand
from mahj.tests.conftest import grant


HOST = 'test.example.com'


@pytest.fixture
def client_():
    c = Client()
    c.defaults['HTTP_HOST'] = HOST
    return c


@pytest.fixture
def staff_user(tournament):
    u = User.objects.create_user('boss', password='pw')
    grant(u, tournament['tenant'], admin=True)
    return u


def _hand_count(tournament, round_nb, table_nb):
    return Hand.objects.filter(
        tenant=tournament['tenant'], round_nb=round_nb, table_nb=table_nb,
    ).count()


class TestDetailedScoresReadOnly:
    def test_unplayed_table_creates_no_rows(self, client_, tournament):
        # Round 3 has seats but no hands in the fixture; opening the modal
        # must not INSERT the 17 placeholder rows (regression guard).
        assert _hand_count(tournament, 3, 1) == 0
        resp = client_.get('/detailed_scores_3_1')
        assert resp.status_code == 200
        assert _hand_count(tournament, 3, 1) == 0

    def test_played_table_creates_no_rows(self, client_, tournament):
        before = _hand_count(tournament, 1, 1)
        assert before > 0
        resp = client_.get('/detailed_scores_1_1')
        assert resp.status_code == 200
        assert _hand_count(tournament, 1, 1) == before

    def test_renders_player_names_and_points(self, client_, tournament):
        resp = client_.get('/detailed_scores_1_1')
        body = resp.content.decode()
        # A seated player's first name and the hidden by/from cells are present.
        assert 'Round 1' in body
        assert 'id="by_1"' in body and 'id="from_1"' in body

    def test_no_nplus1_on_players(self, client_, tournament, django_assert_max_num_queries):
        # seats + one bulk players-attach query + hands + tenant resolve + the
        # reveal gate's fixed lookups (tournament, complete/published/reveal) — a
        # constant number that must not grow with the 4 seated players. Pre-fix
        # the player query did +1 per player.
        with django_assert_max_num_queries(8):
            client_.get('/detailed_scores_1_1')


class TestDetailedScoresPenalties:
    """Penalties surface on the public per-table detail only when at least one is
    non-zero; they are score-sheet figures, never the official MP."""

    def _first_seat(self, tournament, round_nb, table_nb):
        from mahj.models import Seat
        return Seat.objects.filter(
            tenant=tournament['tenant'], round_nb=round_nb, table_nb=table_nb,
        ).order_by('wind').first()

    def test_penalty_row_shown_when_set(self, client_, tournament):
        from django.core.cache import cache
        seat = self._first_seat(tournament, 1, 1)
        seat.penalty = -10
        seat.save(update_fields=['penalty'])
        cache.clear()  # the modal caches rendered HTML; render fresh
        body = client_.get('/detailed_scores_1_1').content.decode()
        assert 'id="pen_1"' in body
        assert '-10' in body

    def test_no_penalty_row_when_all_zero(self, client_, tournament):
        from django.core.cache import cache
        cache.clear()
        body = client_.get('/detailed_scores_1_1').content.decode()
        assert 'id="pen_1"' not in body


class TestDetailedScoresReveal:
    """The raw per-table grid must honour the same reveal masking as the
    standings: a round held back for the ceremony (here round 3, unpublished) is
    a placeholder for the public but fully visible to staff. Without this the
    final round leaks before the prize-giving (L2)."""

    def test_public_sees_placeholder_for_unrevealed_round(self, client_, tournament):
        cache.clear()
        body = client_.get('/detailed_scores_3_1').content.decode()
        assert 'Results not yet revealed' in body
        # None of the score-grid markers leak through.
        assert 'id="by_1"' not in body

    def test_public_still_sees_published_round(self, client_, tournament):
        cache.clear()
        body = client_.get('/detailed_scores_1_1').content.decode()
        assert 'Results not yet revealed' not in body
        assert 'id="by_1"' in body

    def test_staff_sees_unrevealed_round(self, client_, tournament, staff_user):
        cache.clear()
        client_.force_login(staff_user)
        body = client_.get('/detailed_scores_3_1').content.decode()
        assert 'Results not yet revealed' not in body
        assert 'id="by_1"' in body

    def test_staff_view_not_served_from_public_cache(self, client_, tournament, staff_user):
        # Cache is keyed on is_admin: a public placeholder cached first must not
        # be served back to staff (regression guard for the cache-key fix).
        cache.clear()
        public = client_.get('/detailed_scores_3_1').content.decode()
        assert 'Results not yet revealed' in public
        client_.force_login(staff_user)
        staff = client_.get('/detailed_scores_3_1').content.decode()
        assert 'Results not yet revealed' not in staff
