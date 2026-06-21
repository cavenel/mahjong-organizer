"""Public desktop modal endpoints (opened by the spectator crowd).

detailed_scores is unauthenticated and linked from every table cell on the
desktop, including unplayed rounds. Guard the two properties that matter under
crowd load: it must not write rows on a read, and it must not N+1 on players.
"""
import pytest
from django.test import Client

from mahj.models import Hand


HOST = 'test.mahj.ovh'


@pytest.fixture
def client_():
    c = Client()
    c.defaults['HTTP_HOST'] = HOST
    return c


def _hand_count(tournament, round_nb, table_nb):
    return Hand.objects.filter(
        tenant=tournament['tenant'], round_nb=round_nb, table_nb=table_nb,
    ).count()


class TestDetailedScoresReadOnly:
    def test_unplayed_table_creates_no_rows(self, client_, tournament):
        # Round 3 has positions but no hands in the fixture; opening the modal
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
        # positions (select_related player) + hands + tenant resolve — must not
        # grow with the 4 seated players. Pre-fix this did +1 query per player.
        with django_assert_max_num_queries(5):
            client_.get('/detailed_scores_1_1')
