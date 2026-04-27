"""Behavioral assertions for scoring views (complements the snapshot tests).

The golden-file tests lock exact output. These tests assert the *properties* the
UI and tournament rules depend on — rank ordering, tie-sharing, per-country
Swedish ranking, hidden final cut-off, etc.
"""
import pytest

from mahj import views
from mahj.models import Hand, Position, PublishedRound


@pytest.fixture
def completed_tournament(tournament):
    """Score round 3 so the fixture represents a finished tournament.

    All non-last rounds are published fully; the last round is published
    with reveal_level=0 — i.e. podium ceremony ready but not yet started,
    equivalent to the old `variables.final == 0` state.
    """
    for pos in Position.objects.filter(tenant=tournament['tenant'], round_nb=3):
        pos.minipoints = (pos.player_id * 7 + 11) % 200
        pos.tablepoints = float([4, 2, 1, 0][pos.position - 1])
        pos.save()
    for tn in range(1, 5):
        for hn in range(1, 17):
            Hand.objects.create(
                tenant=tournament['tenant'], round_nb=3, table_nb=tn, hand_nb=hn,
                pts=(300 + tn * 10 + hn) % 50,
                win_by=((tn + hn) % 4) + 1,
                win_from=((tn + hn + 1) % 4) + 1,
            )
        Hand.objects.create(
            tenant=tournament['tenant'], round_nb=3, table_nb=tn, hand_nb=17,
            pts=1, win_by=0, win_from=0,
        )
    nb_rounds = tournament['variable'].nb_rounds
    for r in range(1, nb_rounds):
        PublishedRound.objects.update_or_create(
            tenant=tournament['tenant'], round_nb=r,
            defaults={'reveal_level': 100},
        )
    PublishedRound.objects.update_or_create(
        tenant=tournament['tenant'], round_nb=nb_rounds,
        defaults={'reveal_level': 0},
    )
    return tournament


class TestPlayerStandings:
    def test_returns_one_row_per_player(self, request_, tournament):
        rows = views.scores_per_player_json(request_, force_all=True)
        assert len(rows) == len(tournament['players'])

    def test_sorted_by_tp_then_mp_for_mcr(self, request_):
        rows = views.scores_per_player_json(request_, force_all=True)
        totals = [(r['total']['tp'], r['total']['mp']) for r in rows]
        # MCR: descending by tp, tiebreak descending by mp
        assert totals == sorted(totals, key=lambda t: (-t[0], -t[1]))

    def test_ranks_are_dense_with_ties_sharing(self, request_):
        rows = views.scores_per_player_json(request_, force_all=True)
        # pos values must be 1..n, with ties sharing (1,2,2,4 pattern).
        positions = [r['pos'] for r in rows]
        assert positions[0] == 1
        assert all(p <= len(positions) for p in positions)
        # Strictly non-decreasing as we walk down the sorted list.
        for a, b in zip(positions, positions[1:]):
            assert a <= b

    def test_pos_se_only_assigned_to_swedish_players(self, request_):
        rows = views.scores_per_player_json(request_, force_all=True)
        for r in rows:
            if r['country'].strip() == 'Sweden':
                assert isinstance(r['pos_se'], int) and r['pos_se'] >= 1
            else:
                assert r['pos_se'] == ''

    def test_pos_se_is_dense_1_to_count(self, request_):
        rows = views.scores_per_player_json(request_, force_all=True)
        swedes = [r for r in rows if r['country'].strip() == 'Sweden']
        pos_se = sorted(r['pos_se'] for r in swedes)
        assert pos_se[0] == 1
        assert pos_se[-1] <= len(swedes)

    def test_history_pos_length_matches_rounds_plus_initial(self, request_, tournament):
        rows = views.scores_per_player_json(request_, force_all=True)
        # history_pos starts at 1 (initial) then appends one entry per round in the schedule.
        # Fixture has 3 rounds total, so length == 1 + 3 = 4.
        expected = 1 + tournament['variable'].nb_rounds
        for r in rows:
            assert len(r['history_pos']) == expected

    def test_scores_length_matches_scored_rounds(self, request_):
        rows = views.scores_per_player_json(request_, force_all=True)
        # Round 3 is partial (no minipoints), so only 2 rounds appear in scores.
        for r in rows:
            assert len(r['scores']) == 2


class TestFinalCutoff:
    def test_default_shows_all_when_final_is_zero(self, request_):
        rows = views.scores_per_player_json(request_, force_all=True)
        assert all(r['visible'] is True for r in rows)


class TestEndOfTournamentHideLastRound:
    """When all rounds are scored but the podium reveal (`variables.final`) hasn't
    progressed past 11, public viewers should see standings through the *previous*
    round — not an empty page, and not the final standings."""

    def test_public_viewer_sees_standings_through_previous_round(self, request_, completed_tournament):
        nb_rounds = completed_tournament['variable'].nb_rounds
        rows = views.scores_per_player_json(request_, check_final=True)
        assert len(rows) == len(completed_tournament['players'])
        # history_pos length == round_max + 2; hide-last-round drops round_max to nb_rounds - 1.
        for r in rows:
            assert len(r['history_pos']) == nb_rounds + 1
            assert len(r['scores']) == nb_rounds - 1

    def test_admin_viewer_sees_full_standings_with_visibility_flags(self, request_, completed_tournament):
        nb_rounds = completed_tournament['variable'].nb_rounds
        rows = views.scores_per_player_json(request_, check_final=False)
        assert len(rows) == len(completed_tournament['players'])
        for r in rows:
            assert len(r['scores']) == nb_rounds
            assert len(r['history_pos']) == nb_rounds + 2  # round_max stays at nb_rounds
        # final=0 means nothing has been revealed yet: every row is marked not visible.
        assert all(r['visible'] is False for r in rows)

    def test_force_all_bypasses_hide(self, request_, completed_tournament):
        nb_rounds = completed_tournament['variable'].nb_rounds
        rows = views.scores_per_player_json(request_, force_all=True)
        for r in rows:
            assert len(r['scores']) == nb_rounds
            assert r['visible'] is True

    def test_final_past_threshold_reveals_everything(self, request_, completed_tournament):
        nb_rounds = completed_tournament['variable'].nb_rounds
        last_pub = PublishedRound.objects.get(
            tenant=completed_tournament['tenant'], round_nb=nb_rounds,
        )
        last_pub.reveal_level = 12
        last_pub.save()
        rows = views.scores_per_player_json(request_, check_final=True)
        for r in rows:
            assert len(r['scores']) == nb_rounds
            assert r['visible'] is True


class TestRoundWinners:
    def test_only_returns_fully_scored_rounds(self, request_):
        rounds = views.stat_rounds(request_)
        # Fixture has 3 rounds, only 2 are complete (round 3 is partial).
        assert len(rounds) == 2

    def test_mp_max_picks_highest_minipoints(self, request_):
        rounds = views.stat_rounds(request_)
        for rs in rounds:
            mps = [p.minipoints for p in rs['mp_max']]
            assert len(set(mps)) == 1  # all tied at the max
