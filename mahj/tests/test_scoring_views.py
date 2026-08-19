"""Behavioral assertions for scoring views (complements the snapshot tests).

The golden-file tests lock exact output. These tests assert the *properties* the
UI and tournament rules depend on — rank ordering, tie-sharing, per-country
Swedish ranking, hidden final cut-off, etc.
"""
import pytest

from mahj import views
from mahj.models import Hand, Player, Seat, ScoreSheet, PublishedRound, TournamentSettings
from mahj.scoring import player_extra_stats, public_round_max, team_extra_stats


@pytest.fixture
def completed_tournament(tournament):
    """Score round 3 so the fixture represents a finished tournament.

    All non-last rounds are published fully; the last round is published
    withheld — i.e. podium ceremony ready but not yet started.
    """
    for pos in Seat.objects.filter(tenant=tournament['tenant'], round_nb=3):
        pos.minipoints = (pos.draw_number * 7 + 11) % 200
        pos.tablepoints = float([4, 2, 1, 0][pos.wind - 1])
        pos.save()
    for tn in range(1, 5):
        for hn in range(1, 17):
            pts = (300 + tn * 10 + hn) % 50
            if pts > 0:
                Hand.objects.create(
                    tenant=tournament['tenant'], round_nb=3, table_nb=tn, hand_nb=hn,
                    points=pts,
                    win_by=((tn + hn) % 4) + 1,
                    win_from=((tn + hn + 1) % 4) + 1,
                )
            else:
                Hand.objects.create(
                    tenant=tournament['tenant'], round_nb=3, table_nb=tn, hand_nb=hn,
                    points=0, win_by=0, win_from=None,
                )
        ScoreSheet.objects.create(
            tenant=tournament['tenant'], round_nb=3, table_nb=tn, validated=True)
    nb_rounds = tournament['settings'].nb_rounds
    for r in range(1, nb_rounds):
        PublishedRound.objects.update_or_create(
            tenant=tournament['tenant'], round_nb=r,
            defaults={'withheld': False},
        )
    PublishedRound.objects.update_or_create(
        tenant=tournament['tenant'], round_nb=nb_rounds,
        defaults={'withheld': True},
    )
    return tournament


class TestPlayerStandings:
    def test_returns_one_row_per_player(self, request_, tournament):
        rows = views.scores_per_player_rows(request_, full_view=True)
        assert len(rows) == len(tournament['players'])

    def test_sorted_by_tp_then_mp_for_mcr(self, request_):
        rows = views.scores_per_player_rows(request_, full_view=True)
        totals = [(r['total']['tp'], r['total']['mp']) for r in rows]
        # MCR: descending by tp, tiebreak descending by mp
        assert totals == sorted(totals, key=lambda t: (-t[0], -t[1]))

    def test_sorted_by_mp_only_for_riichi(self, request_riichi):
        rows = views.scores_per_player_rows(request_riichi, full_view=True)
        mps = [r['total']['mp'] for r in rows]
        # Riichi ranks on minipoints alone — strictly non-increasing, regardless
        # of table points (which must not influence the order).
        assert mps == sorted(mps, reverse=True)

    def test_ranks_are_dense_with_ties_sharing(self, request_):
        rows = views.scores_per_player_rows(request_, full_view=True)
        # pos values must be 1..n, with ties sharing (1,2,2,4 pattern).
        ranks = [r['pos'] for r in rows]
        assert ranks[0] == 1
        assert all(p <= len(ranks) for p in ranks)
        # Strictly non-decreasing as we walk down the sorted list.
        for a, b in zip(ranks, ranks[1:]):
            assert a <= b

    def test_pos_se_only_assigned_to_swedish_players(self, request_):
        rows = views.scores_per_player_rows(request_, full_view=True)
        for r in rows:
            if r['country'].strip() == 'Sweden':
                assert isinstance(r['pos_se'], int) and r['pos_se'] >= 1
            else:
                assert r['pos_se'] == ''

    def test_pos_se_is_dense_1_to_count(self, request_):
        rows = views.scores_per_player_rows(request_, full_view=True)
        swedes = [r for r in rows if r['country'].strip() == 'Sweden']
        pos_se = sorted(r['pos_se'] for r in swedes)
        assert pos_se[0] == 1
        assert pos_se[-1] <= len(swedes)

    def test_no_national_ranking_when_home_country_unset(self, request_, tournament):
        """Neutral default: with no home nation configured, no national
        sub-ranking is computed — pos_se stays blank for every player."""
        v = tournament['settings']
        v.home_country = ''
        v.save()  # post_save signal busts the cached settings
        rows = views.scores_per_player_rows(request_, full_view=True)
        assert all(r['pos_se'] == '' for r in rows)

    def test_history_pos_length_matches_rounds_plus_initial(self, request_, tournament):
        rows = views.scores_per_player_rows(request_, full_view=True)
        # history_pos starts at 1 (initial) then appends one entry per round in the schedule.
        # Fixture has 3 rounds total, so length == 1 + 3 = 4.
        expected = 1 + tournament['settings'].nb_rounds
        for r in rows:
            assert len(r['history_pos']) == expected

    def test_scores_length_matches_scored_rounds(self, request_):
        rows = views.scores_per_player_rows(request_, full_view=True)
        # Round 3 is partial (no minipoints), so only 2 rounds appear in scores.
        for r in rows:
            assert len(r['scores']) == 2


def _seat_one_table(tenant, seats, rules='MCR'):
    """Create a TournamentSettings + one 4-seat table. `seats` is
    [(minipoints, tablepoints), …] in seat order. Returns (tournament, [players])."""
    tournament = TournamentSettings.objects.create(
        tenant=tenant, welcome='W', title='T', fullname='F',
        nb_rounds=1, rules=rules,
    )
    players = []
    for i, (mp, tp) in enumerate(seats):
        p = Player.objects.create(
            tenant=tenant, draw_number=i + 1, full_name=f'P{i + 1} L',
            first_name=f'P{i + 1}', country='Sweden', EMA_ID=f'E{i}',
        )
        Seat.objects.create(
            tenant=tenant, round_nb=1, table_nb=1, wind=i + 1,
            draw_number=p.draw_number, minipoints=mp, tablepoints=tp,
        )
        players.append(p)
    return tournament, players


# A 2-way tie for 1st (both mp=100 -> averaged tp 3.0), then a clean 3rd
# (mp=50, tp 1.0) and 4th (mp=20, tp 0.0).
_TIE_SEATS = [(100, 3.0), (100, 3.0), (50, 1.0), (20, 0.0)]


class TestPlacementStats:
    """player_extra_stats placement rates — especially MCR tied tables, whose
    averaged table points (3.0, 1.5, 0.5, …) used to match no fixed-TP key and
    silently dropped the whole round from the counts."""

    def test_tied_first_place_round_is_counted_not_dropped(self, tenant):
        tournament, players = _seat_one_table(tenant, _TIE_SEATS, 'MCR')
        stats = player_extra_stats(tenant, players[0], tournament)
        # The tied-for-1st player's round must be counted (was dropped before).
        assert stats['total_rounds'] == 1
        by_place = {p['place']: p for p in stats['placement']}
        assert by_place[1]['count'] == 1
        assert by_place[1]['rate_pct'] == 100
        assert by_place[2]['count'] == 0

    def test_both_tied_players_share_first(self, tenant):
        tournament, players = _seat_one_table(tenant, _TIE_SEATS, 'MCR')
        for tied in (players[0], players[1]):
            by_place = {p['place']: p['count']
                        for p in player_extra_stats(tenant, tied, tournament)['placement']}
            assert by_place == {1: 1, 2: 0, 3: 0, 4: 0}

    def test_lower_seats_keep_their_true_place(self, tenant):
        tournament, players = _seat_one_table(tenant, _TIE_SEATS, 'MCR')
        # mp=50 sits behind two tied leaders -> 3rd; mp=20 -> 4th.
        third = {p['place']: p['count']
                 for p in player_extra_stats(tenant, players[2], tournament)['placement']}
        fourth = {p['place']: p['count']
                  for p in player_extra_stats(tenant, players[3], tournament)['placement']}
        assert third == {1: 0, 2: 0, 3: 1, 4: 0}
        assert fourth == {1: 0, 2: 0, 3: 0, 4: 1}


class TestPlacementStatsRiichi:
    """Riichi placement rates: ranked within the table by minipoints, not table
    points. Same tie-sharing and round-counting guarantees as MCR."""

    def test_tied_first_place_round_is_counted_not_dropped(self, tenant):
        tournament, players = _seat_one_table(tenant, _TIE_SEATS, 'Riichi')
        stats = player_extra_stats(tenant, players[0], tournament)
        assert stats['total_rounds'] == 1
        by_place = {p['place']: p for p in stats['placement']}
        assert by_place[1]['count'] == 1
        assert by_place[1]['rate_pct'] == 100
        assert by_place[2]['count'] == 0

    def test_both_tied_players_share_first(self, tenant):
        tournament, players = _seat_one_table(tenant, _TIE_SEATS, 'Riichi')
        for tied in (players[0], players[1]):
            by_place = {p['place']: p['count']
                        for p in player_extra_stats(tenant, tied, tournament)['placement']}
            assert by_place == {1: 1, 2: 0, 3: 0, 4: 0}

    def test_lower_seats_keep_their_true_place(self, tenant):
        tournament, players = _seat_one_table(tenant, _TIE_SEATS, 'Riichi')
        third = {p['place']: p['count']
                 for p in player_extra_stats(tenant, players[2], tournament)['placement']}
        fourth = {p['place']: p['count']
                  for p in player_extra_stats(tenant, players[3], tournament)['placement']}
        assert third == {1: 0, 2: 0, 3: 1, 4: 0}
        assert fourth == {1: 0, 2: 0, 3: 0, 4: 1}

    def test_placement_follows_minipoints_not_table_points(self, tenant):
        # Table points and minipoints disagree: the seat with the most table
        # points has the fewest minipoints. Riichi must place by minipoints.
        seats = [(10, 4.0), (40, 2.0), (70, 1.0), (100, 0.0)]
        tournament, players = _seat_one_table(tenant, seats, 'Riichi')
        place_of = lambda pl: next(
            p['place'] for p in player_extra_stats(tenant, pl, tournament)['placement']
            if p['count'])
        # mp=100 (tp 0.0) is 1st; mp=10 (tp 4.0) is 4th — the reverse of MCR.
        assert place_of(players[3]) == 1
        assert place_of(players[2]) == 2
        assert place_of(players[1]) == 3
        assert place_of(players[0]) == 4


class TestWinLossStatsValidationGate:
    """Win/loss hand stats only count hands from a validated score sheet
    (a ScoreSheet with validated=True), like the detailed-hands modal — an
    un-validated sheet (e.g. freshly scanned, not yet human-checked) must not
    feed the rates."""

    def _table_with_hands(self, tenant):
        tournament, players = _seat_one_table(tenant, _TIE_SEATS, 'MCR')
        # Two real hands won by seat 1 (players[0]) on a discard from seat 2.
        for hn, pts in ((1, 20), (2, 30)):
            Hand.objects.create(tenant=tenant, round_nb=1, table_nb=1, hand_nb=hn,
                                points=pts, win_by=1, win_from=2)
        return tournament, players

    def test_unvalidated_sheet_hands_are_not_counted(self, tenant):
        tournament, players = self._table_with_hands(tenant)
        # No validated ScoreSheet -> table is not validated.
        stats = player_extra_stats(tenant, players[0], tournament)
        assert stats['total_hands'] == 0

    def test_validated_sheet_hands_are_counted(self, tenant):
        tournament, players = self._table_with_hands(tenant)
        ScoreSheet.objects.create(tenant=tenant, round_nb=1, table_nb=1, validated=True)
        stats = player_extra_stats(tenant, players[0], tournament)
        assert stats['total_hands'] == 2
        by_label = {s['label']: s['count'] for s in stats['hand_stats']}
        assert by_label['Win by discard'] == 2

    def test_draw_hand_counts_as_played(self, tenant):
        tournament, players = self._table_with_hands(tenant)  # hands 1,2 discard-win seat 1
        # A validated sheet stores exactly the hands played: hand 3 is a genuine
        # draw (win_by 0, nobody won), hand 4 a decided hand. There is no trailing
        # unplayed (win_by NULL) row — the entry/prune flow never persists one.
        Hand.objects.create(tenant=tenant, round_nb=1, table_nb=1, hand_nb=3,
                            points=0, win_by=0, win_from=None)
        Hand.objects.create(tenant=tenant, round_nb=1, table_nb=1, hand_nb=4,
                            points=25, win_by=1, win_from=2)
        ScoreSheet.objects.create(tenant=tenant, round_nb=1, table_nb=1, validated=True)
        stats = player_extra_stats(tenant, players[0], tournament)
        assert stats['total_hands'] == 4  # 3 decided + 1 draw
        by_label = {s['label']: s['count'] for s in stats['hand_stats']}
        assert by_label['Draw'] == 1
        assert by_label['Win by discard'] == 3

    def test_team_stats_also_gate_on_validation(self, tenant):
        tournament, players = self._table_with_hands(tenant)
        for p in players:
            p.team = 'Reds'
            p.save()
        assert team_extra_stats(tenant, 'Reds', tournament)['total_hands'] == 0
        ScoreSheet.objects.create(tenant=tenant, round_nb=1, table_nb=1, validated=True)
        # All 4 team members sit at this table, so team stats see each hand from
        # every member's seat: 2 hands x 4 players = 8 player-hand observations.
        assert team_extra_stats(tenant, 'Reds', tournament)['total_hands'] == 8


class TestExtraStatsMaskFinalRound:
    """The player/team modal's placement and win/loss cards must honour the same
    end-of-tournament masking as the modal's score grid. While the final round is
    scored but withheld for the ceremony (reveal=0), it must stay out of these
    cards for public viewers — otherwise the placement/hand-count jump leaks the
    champion's final table before the reveal. Admin viewers still see every round.

    `completed_tournament` is exactly that state: all 3 rounds scored + validated,
    rounds 1-2 published, the final round published at reveal=0.
    """

    def test_public_cutoff_drops_the_withheld_final_round(self, completed_tournament):
        tenant, tournament = completed_tournament['tenant'], completed_tournament['settings']
        assert public_round_max(tenant, tournament, full_view=False) == tournament.nb_rounds - 1
        assert public_round_max(tenant, tournament, full_view=True) == tournament.nb_rounds

    def test_public_player_cards_exclude_final_round(self, completed_tournament):
        tenant, tournament = completed_tournament['tenant'], completed_tournament['settings']
        player = completed_tournament['players'][0]
        public = player_extra_stats(
            tenant, player, tournament,
            max_round=public_round_max(tenant, tournament, full_view=False),
        )
        admin = player_extra_stats(tenant, player, tournament, max_round=None)
        # The final round is folded in for admin but withheld from the public.
        assert admin['total_rounds'] == tournament.nb_rounds
        assert public['total_rounds'] == tournament.nb_rounds - 1
        # Validated final-round hands count for admin, not for the public viewer.
        assert public['total_hands'] < admin['total_hands']

    def test_public_team_cards_exclude_final_round(self, completed_tournament):
        tenant, tournament = completed_tournament['tenant'], completed_tournament['settings']
        player = completed_tournament['players'][0]
        player.team = 'Reds'
        player.save()
        public = team_extra_stats(
            tenant, 'Reds', tournament,
            max_round=public_round_max(tenant, tournament, full_view=False),
        )
        admin = team_extra_stats(tenant, 'Reds', tournament, max_round=None)
        assert admin['total_rounds'] == tournament.nb_rounds
        assert public['total_rounds'] == tournament.nb_rounds - 1
        assert public['total_hands'] < admin['total_hands']


class TestEndOfTournamentHideLastRound:
    """When all rounds are scored but the podium reveal (`tournament.final`) hasn't
    progressed past 11, public viewers should see standings through the *previous*
    round — not an empty page, and not the final standings."""

    def test_public_viewer_sees_standings_through_previous_round(self, request_, completed_tournament):
        nb_rounds = completed_tournament['settings'].nb_rounds
        rows = views.scores_per_player_rows(request_, full_view=False)
        assert len(rows) == len(completed_tournament['players'])
        # history_pos length == round_max + 2; hide-last-round drops round_max to nb_rounds - 1.
        for r in rows:
            assert len(r['history_pos']) == nb_rounds + 1
            assert len(r['scores']) == nb_rounds - 1

    def test_full_view_sees_all_rounds_in_suspense(self, request_, completed_tournament):
        """A full view (admin / ceremony / print) bypasses the public cutoff and
        sees every scored round even while the final round is withheld."""
        nb_rounds = completed_tournament['settings'].nb_rounds
        rows = views.scores_per_player_rows(request_, full_view=True)
        assert len(rows) == len(completed_tournament['players'])
        for r in rows:
            assert len(r['scores']) == nb_rounds
            assert len(r['history_pos']) == nb_rounds + 2  # round_max stays at nb_rounds

    def test_full_view_bypasses_hide(self, request_, completed_tournament):
        nb_rounds = completed_tournament['settings'].nb_rounds
        rows = views.scores_per_player_rows(request_, full_view=True)
        for r in rows:
            assert len(r['scores']) == nb_rounds

    def test_final_past_threshold_reveals_everything(self, request_, completed_tournament):
        nb_rounds = completed_tournament['settings'].nb_rounds
        last_pub = PublishedRound.objects.get(
            tenant=completed_tournament['tenant'], round_nb=nb_rounds,
        )
        last_pub.withheld = False  # final round revealed to everyone
        last_pub.save()
        rows = views.scores_per_player_rows(request_, full_view=False)
        for r in rows:
            assert len(r['scores']) == nb_rounds


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


class TestOverallWinnersMaskFinalRound:
    """The 'Overall — after N rounds' cards must honour the same end-of-tournament
    masking as the per-round cards. While the final round is scored but not yet
    published (ceremony pending), its hands/scores must stay out of the overall
    roll-up for public viewers (full_view=False) — otherwise the projector/desktop
    leaks the champion's biggest hand/top game before the ceremony reveals it.
    Admin/ceremony (full_view=True) still see everything.
    """

    @pytest.fixture
    def suspense_tournament(self, completed_tournament):
        # Earlier rounds are published; the *final* round is fully scored but has
        # no PublishedRound row yet — the exact pre-ceremony state where the leak
        # used to occur. Spike one final-round seat + one self-draw hand to the
        # unique overall best so a leak would be unmistakable.
        nb_rounds = completed_tournament['settings'].nb_rounds
        tenant = completed_tournament['tenant']
        PublishedRound.objects.filter(tenant=tenant, round_nb=nb_rounds).delete()
        pos = Seat.objects.filter(tenant=tenant, round_nb=nb_rounds).first()
        pos.minipoints = 100000
        pos.save()
        h = Hand.objects.filter(tenant=tenant, round_nb=nb_rounds, hand_nb=1).first()
        h.points, h.win_by, h.win_from = 100000, 1, None  # self-draw (no discarder)
        h.save()
        return completed_tournament

    def test_public_overall_excludes_unpublished_final_round(self, request_, suspense_tournament):
        nb_rounds = suspense_tournament['settings'].nb_rounds
        overall = views.stat_all_rounds(request_, full_view=False)
        # Roll-up still has earlier-round data, but nothing from the withheld final.
        assert overall['mp_max']
        assert all(p.round_nb < nb_rounds for p in overall['mp_max'])
        assert all(h['points'] < 100000 for h in overall['sd_hand_max'])

    def test_admin_overall_includes_final_round(self, request_, suspense_tournament):
        nb_rounds = suspense_tournament['settings'].nb_rounds
        overall = views.stat_all_rounds(request_, full_view=True)
        assert any(
            p.round_nb == nb_rounds and p.minipoints == 100000 for p in overall['mp_max']
        )
        assert any(h['points'] == 100000 for h in overall['sd_hand_max'])

    def test_full_view_is_unmasked_for_ceremony(self, request_, suspense_tournament):
        # ceremony.py calls stat_all_rounds(request, full_view=True) and must keep
        # seeing the final round (the reveal surface).
        nb_rounds = suspense_tournament['settings'].nb_rounds
        overall = views.stat_all_rounds(request_, full_view=True)
        assert any(p.round_nb == nb_rounds for p in overall['mp_max'])


class TestStatCardsConsistentWhenFinalPublishedWithheld:
    """Once the admin presses "publish last round" the final round gets a
    PublishedRound row at reveal=0 (ceremony pending). In that window the
    standings and detail modals show rounds 1..n-1; the per-round / overall
    winner cards must show the *same* rounds — not vanish entirely. `round_winners`
    used to special-case this state and return [], disagreeing with every other
    public surface. They now share `public_round_max`.

    `completed_tournament` is exactly this state (final round published, reveal=0).
    """

    def test_public_round_cards_show_through_previous_round(self, request_, completed_tournament):
        nb_rounds = completed_tournament['settings'].nb_rounds
        rounds = views.stat_rounds(request_, full_view=False)
        # Not [] — rounds 1..n-1 are shown, matching the standings/modal cutoff.
        assert len(rounds) == nb_rounds - 1
        assert len(rounds) == public_round_max(
            completed_tournament['tenant'], completed_tournament['settings'], full_view=False
        )

    def test_public_overall_cards_exclude_final_but_are_not_empty(self, request_, completed_tournament):
        nb_rounds = completed_tournament['settings'].nb_rounds
        overall = views.stat_all_rounds(request_, full_view=False)
        assert overall['mp_max']
        assert all(p.round_nb < nb_rounds for p in overall['mp_max'])

    def test_admin_still_sees_the_final_round(self, request_, completed_tournament):
        nb_rounds = completed_tournament['settings'].nb_rounds
        rounds = views.stat_rounds(request_, full_view=True)
        assert len(rounds) == nb_rounds


class TestNoRoundsFallback:
    """Before any round is scored, the standings would read "Scores after round 0"
    over an all-zero table — meaningless, so the view falls back to the schedule."""

    @pytest.fixture
    def unscored_tournament(self, tournament):
        # Wipe every scored round: no hands, and Seats carry no points.
        Hand.objects.filter(tenant=tournament['tenant']).delete()
        Seat.objects.filter(tenant=tournament['tenant']).update(
            minipoints=None, tablepoints=None,
        )
        PublishedRound.objects.filter(tenant=tournament['tenant']).delete()
        return tournament

    def test_detailed_falls_back_to_schedule(self, request_, unscored_tournament):
        html = views.render_scores(request_, "detailed", None).content.decode()
        assert '<title>Schedule</title>' in html
        assert 'Scores after round' not in html

    def test_totals_falls_back_to_schedule(self, request_, unscored_tournament):
        html = views.render_scores(request_, "totals", 1).content.decode()
        assert '<title>Schedule</title>' in html
        assert 'Scores after round' not in html

    def test_scores_render_normally_once_a_round_is_scored(self, request_, tournament):
        # The seeded fixture has two complete rounds, so scores still show.
        html = views.render_scores(request_, "detailed", None).content.decode()
        assert '<title>Scores</title>' in html


class TestNextRoundBadge:
    """The standings screen shows a corner badge with the schedule time of the
    round about to be played, e.g. "Round 3: 12:00". The seeded fixture has two
    scored rounds (round 3 partial), so the next round is 3, and the schedule
    seeds "Round N" rows timed at 10:00/11:00/12:00."""

    def test_badge_shows_next_round_schedule_time(self, request_, tournament):
        html = views.render_scores(request_, "detailed", None).content.decode()
        assert 'Scores after round 2' in html
        assert 'Round 3:' in html
        assert '12:00' in html

    def test_wall_clock_renders_regardless_of_round(self, request_, tournament):
        # The live wall clock is always present (JS fills it in the browser).
        html = views.render_scores(request_, "detailed", None).content.decode()
        assert 'id="wallclock"' in html

    def test_badge_hidden_once_final_round_is_played(self, request_, completed_tournament):
        # Round 3 fully played and published (not withheld): no next round to show,
        # but the wall clock stays.
        PublishedRound.objects.update_or_create(
            tenant=completed_tournament['tenant'], round_nb=3,
            defaults={'withheld': False},
        )
        html = views.render_scores(request_, "detailed", None).content.decode()
        assert 'Scores after round 3' in html
        assert 'Round 4:' not in html
        assert 'id="wallclock"' in html

    def test_badge_hidden_while_awaiting_ceremony(self, request_, completed_tournament):
        # Final round withheld for the ceremony → holding screen, no badge.
        html = views.render_scores(request_, "detailed", None).content.decode()
        assert 'Waiting for the ceremony to start' in html
        assert 'Round 4:' not in html


class TestTeamsView:
    """The "Standings — teams" display (density 'teams') shows the individual
    totals pages first, then extra team-totals pages, rotating through both."""

    @pytest.fixture
    def team_tournament(self, tournament):
        # Split the 16 players into four teams by their round-1 table.
        players = tournament['players']
        for i, p in enumerate(players):
            p.team = 'Team %s' % chr(ord('A') + i % 4)
            p.save()
        return tournament

    def test_teams_view_appends_team_pages(self, request_, team_tournament):
        html = views.render_scores(request_, "teams", None).content.decode()
        assert '<title>Scores</title>' in html
        # Both section headings appear, and at least one team name is rendered.
        assert 'Individuals' in html
        assert 'Teams' in html
        assert 'Team A' in html

    def test_teams_view_without_teams_falls_back_to_individuals(self, request_, tournament):
        # No player has a team → team_standings is empty, so only individual
        # pages render (equivalent to 'totals'); no "Teams" section heading.
        html = views.render_scores(request_, "teams", None).content.decode()
        assert '<title>Scores</title>' in html
        assert '>Teams<' not in html

    def test_teams_view_pins_a_single_page(self, request_, team_tournament):
        # Pinning a page returns exactly that one page (no rotation loop).
        html = views.render_scores(request_, "teams", 1).content.decode()
        assert '<title>Scores</title>' in html
        assert 'scores_page_2' not in html


class TestSpectatorQr:
    """The public-site QR on the score displays is generated locally (segno),
    never fetched from an external service — so a projector behind a captive
    portal / firewall still shows a scannable code (EVENT_REVIEW finding 🔴-1)."""

    def test_detailed_view_renders_inline_svg_qr_not_cdn(self, request_):
        html = views.render_scores(request_, "detailed", 1).content.decode()
        assert '<svg' in html                      # rendered inline, locally
        assert 'api.qrserver.com' not in html      # no external QR service

    def test_totals_view_renders_inline_svg_qr_not_cdn(self, request_):
        html = views.render_scores(request_, "totals", None).content.decode()
        assert '<svg' in html
        assert 'api.qrserver.com' not in html

    def test_qr_helper_encodes_https_spectator_url(self):
        from mahj.views.display import _spectator_qr_svg
        import segno
        svg = _spectator_qr_svg('test')
        assert svg.startswith('<svg')
        # The encoded payload is the HTTPS public site, matching the visible label.
        expected = segno.make('https://test.example.com', error='m').svg_inline(scale=3, border=2)
        assert svg == expected

    def test_qr_helper_empty_without_subdomain(self):
        from mahj.views.display import _spectator_qr_svg
        assert _spectator_qr_svg('') == ''


class TestUnclaimedDrawSlot:
    """An undrawn draw slot (a Seat whose draw_number no Player holds) reads as
    "Player <n>" everywhere a competitor name is shown, rather than blank."""

    def _unclaim(self, tournament, draw_number):
        """Drop the player holding `draw_number`, leaving its seats unclaimed."""
        Player.objects.filter(
            tenant=tournament['tenant'], draw_number=draw_number).delete()

    def test_seat_helpers_fall_back_to_player_label(self, tournament):
        self._unclaim(tournament, 5)
        from mahj.scoring import _attach_players
        seat = _attach_players(tournament['tenant'], list(
            Seat.objects.filter(tenant=tournament['tenant'], draw_number=5)))[0]
        assert seat.player is None
        assert seat.player_name() == 'Player 5'
        assert seat.player_short_name() == 'Player 5'

    def test_slot_rounds_still_builds_a_card_for_the_unclaimed_slot(self, tournament):
        self._unclaim(tournament, 5)
        from mahj.scoring import all_slot_rounds
        rounds = all_slot_rounds(tournament['tenant'])
        # The slot keeps its seats (and so a printable card) even with no player.
        assert 5 in rounds and rounds[5]

    def test_seating_grid_labels_the_unclaimed_seat(self, tournament):
        self._unclaim(tournament, 5)
        from mahj.scoring import tournament_seating
        seating, _ = tournament_seating(
            tournament['tenant'], tournament['settings'], full_view=True)
        names = [
            s['name']
            for r in seating for t in r['tables'] for s in t['seats']
            if s['player'] is None
        ]
        assert 'Player 5' in names

    def test_player_cards_page_shows_player_label(self, request_, tournament):
        self._unclaim(tournament, 5)
        html = views.player_cards(request_).content.decode()
        assert 'Player 5' in html


class TestRiichiHidesHandStats:
    """Riichi records only a per-seat score total (no Hand rows), so the
    hand-by-hand stats and the links to the MCR hand grid are hidden. The two
    score-based cards stay, since they read the seat score that Riichi does keep.
    """

    HAND_SECTIONS = [
        'Highest winning hand', 'Most winning in one game', 'Deal-ins',
        'Luck — self-draws', 'Self-draw rate',
        'Average hands per table', 'Average hand value',
    ]
    KEPT_SECTIONS = ['Highest points in one game', 'Tables finished']

    def test_mcr_shows_all_hand_stats(self, request_):
        html = views.desktop(request_).content.decode()
        for section in self.HAND_SECTIONS + self.KEPT_SECTIONS:
            assert section in html
        assert 'href="detailed_scores_' in html

    def test_riichi_hides_hand_stats_but_keeps_score_stats(self, request_riichi):
        html = views.desktop(request_riichi).content.decode()
        for section in self.HAND_SECTIONS:
            assert section not in html
        for section in self.KEPT_SECTIONS:
            assert section in html

    def test_riichi_has_no_hand_grid_links(self, request_riichi):
        html = views.desktop(request_riichi).content.decode()
        # The clickable links are gone (only the JS click-interceptor string,
        # which never matches without a link, may still mention the prefix).
        assert 'href="detailed_scores_' not in html
        assert ':href="\'detailed_scores_' not in html


class TestPublisherOverviewRiichiColumns:
    """The publisher overview's 'Sheets in progress' / 'Sheets validated' columns
    are hand-sheet concepts (MCR only). Riichi enters a per-seat total and never
    creates or validates a sheet, so both are always 0 — the columns are hidden.
    """

    def _render(self, tenant, tournament):
        from django.template import loader
        from django.test import RequestFactory
        from django.contrib.auth.models import AnonymousUser
        from mahj.views.admin_views import publisher_overview_rows
        rf = RequestFactory()
        req = rf.get('/', HTTP_HOST='test.example.com')
        req.user = AnonymousUser()
        return loader.get_template('mahj/admin_publisher_overview.html').render({
            'rows': publisher_overview_rows(tenant, tournament),
            'tournament': tournament,
            'subdomain': tenant.subdomain,
        }, req)

    def test_mcr_shows_the_sheet_columns(self, tournament):
        html = self._render(tournament['tenant'], tournament['settings'])
        assert '<th>Sheets in progress</th>' in html
        assert '<th>Sheets validated</th>' in html
        assert 'class="cell-inprogress' in html
        assert 'class="cell-validated' in html

    def test_riichi_hides_the_sheet_columns(self, riichi_tournament):
        html = self._render(riichi_tournament['tenant'], riichi_tournament['settings'])
        assert '<th>Sheets in progress</th>' not in html
        assert '<th>Sheets validated</th>' not in html
        assert 'class="cell-inprogress' not in html
        assert 'class="cell-validated' not in html
        # The columns that drive the Riichi workflow are still present.
        assert '<th>Tables scored</th>' in html
        assert '<th>Published</th>' in html
