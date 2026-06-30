"""Behavioral assertions for scoring views (complements the snapshot tests).

The golden-file tests lock exact output. These tests assert the *properties* the
UI and tournament rules depend on — rank ordering, tie-sharing, per-country
Swedish ranking, hidden final cut-off, etc.
"""
import pytest

from mahj import views
from mahj.models import Hand, Player, Position, PublishedRound, Variable
from mahj.scoring import player_extra_stats, public_round_max, team_extra_stats


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

    def test_sorted_by_mp_only_for_riichi(self, request_riichi):
        rows = views.scores_per_player_json(request_riichi, force_all=True)
        mps = [r['total']['mp'] for r in rows]
        # Riichi ranks on minipoints alone — strictly non-increasing, regardless
        # of table points (which must not influence the order).
        assert mps == sorted(mps, reverse=True)

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


def _seat_one_table(tenant, seats, rules='MCR'):
    """Create a Variable + one 4-seat table. `seats` is [(minipoints, tablepoints), …]
    in seat order. Returns (variables, [players])."""
    variables = Variable.objects.create(
        tenant=tenant, welcome='W', title='T', fullname='F',
        nb_rounds=1, rules=rules,
    )
    players = []
    for i, (mp, tp) in enumerate(seats):
        p = Player.objects.create(
            tenant=tenant, rand_id=i + 1, full_name=f'P{i + 1} L',
            first_name=f'P{i + 1}', country='Sweden', EMA_ID=f'E{i}', email='',
        )
        Position.objects.create(
            tenant=tenant, round_nb=1, table_nb=1, position=i + 1,
            player=p, minipoints=mp, tablepoints=tp,
        )
        players.append(p)
    return variables, players


# A 2-way tie for 1st (both mp=100 -> averaged tp 3.0), then a clean 3rd
# (mp=50, tp 1.0) and 4th (mp=20, tp 0.0).
_TIE_SEATS = [(100, 3.0), (100, 3.0), (50, 1.0), (20, 0.0)]


class TestPlacementStats:
    """player_extra_stats placement rates — especially MCR tied tables, whose
    averaged table points (3.0, 1.5, 0.5, …) used to match no fixed-TP key and
    silently dropped the whole round from the counts."""

    def test_tied_first_place_round_is_counted_not_dropped(self, tenant):
        variables, players = _seat_one_table(tenant, _TIE_SEATS, 'MCR')
        stats = player_extra_stats(tenant, players[0], variables)
        # The tied-for-1st player's round must be counted (was dropped before).
        assert stats['total_rounds'] == 1
        by_place = {p['place']: p for p in stats['placement']}
        assert by_place[1]['count'] == 1
        assert by_place[1]['rate_pct'] == 100
        assert by_place[2]['count'] == 0

    def test_both_tied_players_share_first(self, tenant):
        variables, players = _seat_one_table(tenant, _TIE_SEATS, 'MCR')
        for tied in (players[0], players[1]):
            by_place = {p['place']: p['count']
                        for p in player_extra_stats(tenant, tied, variables)['placement']}
            assert by_place == {1: 1, 2: 0, 3: 0, 4: 0}

    def test_lower_seats_keep_their_true_place(self, tenant):
        variables, players = _seat_one_table(tenant, _TIE_SEATS, 'MCR')
        # mp=50 sits behind two tied leaders -> 3rd; mp=20 -> 4th.
        third = {p['place']: p['count']
                 for p in player_extra_stats(tenant, players[2], variables)['placement']}
        fourth = {p['place']: p['count']
                  for p in player_extra_stats(tenant, players[3], variables)['placement']}
        assert third == {1: 0, 2: 0, 3: 1, 4: 0}
        assert fourth == {1: 0, 2: 0, 3: 0, 4: 1}


class TestPlacementStatsRiichi:
    """Riichi placement rates: ranked within the table by minipoints, not table
    points. Same tie-sharing and round-counting guarantees as MCR."""

    def test_tied_first_place_round_is_counted_not_dropped(self, tenant):
        variables, players = _seat_one_table(tenant, _TIE_SEATS, 'Riichi')
        stats = player_extra_stats(tenant, players[0], variables)
        assert stats['total_rounds'] == 1
        by_place = {p['place']: p for p in stats['placement']}
        assert by_place[1]['count'] == 1
        assert by_place[1]['rate_pct'] == 100
        assert by_place[2]['count'] == 0

    def test_both_tied_players_share_first(self, tenant):
        variables, players = _seat_one_table(tenant, _TIE_SEATS, 'Riichi')
        for tied in (players[0], players[1]):
            by_place = {p['place']: p['count']
                        for p in player_extra_stats(tenant, tied, variables)['placement']}
            assert by_place == {1: 1, 2: 0, 3: 0, 4: 0}

    def test_lower_seats_keep_their_true_place(self, tenant):
        variables, players = _seat_one_table(tenant, _TIE_SEATS, 'Riichi')
        third = {p['place']: p['count']
                 for p in player_extra_stats(tenant, players[2], variables)['placement']}
        fourth = {p['place']: p['count']
                  for p in player_extra_stats(tenant, players[3], variables)['placement']}
        assert third == {1: 0, 2: 0, 3: 1, 4: 0}
        assert fourth == {1: 0, 2: 0, 3: 0, 4: 1}

    def test_placement_follows_minipoints_not_table_points(self, tenant):
        # Table points and minipoints disagree: the seat with the most table
        # points has the fewest minipoints. Riichi must place by minipoints.
        seats = [(10, 4.0), (40, 2.0), (70, 1.0), (100, 0.0)]
        variables, players = _seat_one_table(tenant, seats, 'Riichi')
        place_of = lambda pl: next(
            p['place'] for p in player_extra_stats(tenant, pl, variables)['placement']
            if p['count'])
        # mp=100 (tp 0.0) is 1st; mp=10 (tp 4.0) is 4th — the reverse of MCR.
        assert place_of(players[3]) == 1
        assert place_of(players[2]) == 2
        assert place_of(players[1]) == 3
        assert place_of(players[0]) == 4


class TestWinLossStatsValidationGate:
    """Win/loss hand stats only count hands from a validated score sheet (a
    hand_nb=17, pts=1 marker), like the detailed-hands modal — an un-validated
    sheet (e.g. freshly scanned, not yet human-checked) must not feed the rates."""

    def _table_with_hands(self, tenant):
        variables, players = _seat_one_table(tenant, _TIE_SEATS, 'MCR')
        # Two real hands won by seat 1 (players[0]) on a discard from seat 2.
        for hn, pts in ((1, 20), (2, 30)):
            Hand.objects.create(tenant=tenant, round_nb=1, table_nb=1, hand_nb=hn,
                                pts=pts, win_by=1, win_from=2)
        return variables, players

    def test_unvalidated_sheet_hands_are_not_counted(self, tenant):
        variables, players = self._table_with_hands(tenant)
        # No hand_nb=17 pts=1 marker -> table is not validated.
        stats = player_extra_stats(tenant, players[0], variables)
        assert stats['total_hands'] == 0

    def test_validated_sheet_hands_are_counted(self, tenant):
        variables, players = self._table_with_hands(tenant)
        Hand.objects.create(tenant=tenant, round_nb=1, table_nb=1, hand_nb=17,
                            pts=1, win_by=0, win_from=0)
        stats = player_extra_stats(tenant, players[0], variables)
        assert stats['total_hands'] == 2
        by_label = {s['label']: s['count'] for s in stats['hand_stats']}
        assert by_label['Win by discard'] == 2

    def test_team_stats_also_gate_on_validation(self, tenant):
        variables, players = self._table_with_hands(tenant)
        for p in players:
            p.team = 'Reds'
            p.save()
        assert team_extra_stats(tenant, 'Reds', variables)['total_hands'] == 0
        Hand.objects.create(tenant=tenant, round_nb=1, table_nb=1, hand_nb=17,
                            pts=1, win_by=0, win_from=0)
        # All 4 team members sit at this table, so team stats see each hand from
        # every member's seat: 2 hands x 4 players = 8 player-hand observations.
        assert team_extra_stats(tenant, 'Reds', variables)['total_hands'] == 8


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
        tenant, variables = completed_tournament['tenant'], completed_tournament['variable']
        assert public_round_max(tenant, variables, force_all=False) == variables.nb_rounds - 1
        assert public_round_max(tenant, variables, force_all=True) == variables.nb_rounds

    def test_public_player_cards_exclude_final_round(self, completed_tournament):
        tenant, variables = completed_tournament['tenant'], completed_tournament['variable']
        player = completed_tournament['players'][0]
        public = player_extra_stats(
            tenant, player, variables,
            max_round=public_round_max(tenant, variables, force_all=False),
        )
        admin = player_extra_stats(tenant, player, variables, max_round=None)
        # The final round is folded in for admin but withheld from the public.
        assert admin['total_rounds'] == variables.nb_rounds
        assert public['total_rounds'] == variables.nb_rounds - 1
        # Validated final-round hands count for admin, not for the public viewer.
        assert public['total_hands'] < admin['total_hands']

    def test_public_team_cards_exclude_final_round(self, completed_tournament):
        tenant, variables = completed_tournament['tenant'], completed_tournament['variable']
        player = completed_tournament['players'][0]
        player.team = 'Reds'
        player.save()
        public = team_extra_stats(
            tenant, 'Reds', variables,
            max_round=public_round_max(tenant, variables, force_all=False),
        )
        admin = team_extra_stats(tenant, 'Reds', variables, max_round=None)
        assert admin['total_rounds'] == variables.nb_rounds
        assert public['total_rounds'] == variables.nb_rounds - 1
        assert public['total_hands'] < admin['total_hands']


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


class TestOverallWinnersMaskFinalRound:
    """The 'Overall — after N rounds' cards must honour the same end-of-tournament
    masking as the per-round cards. While the final round is scored but not yet
    published (ceremony pending), its hands/scores must stay out of the overall
    roll-up for public viewers (check_final=True) — otherwise the projector/desktop
    leaks the champion's biggest hand/top game before the ceremony reveals it.
    Admin/ceremony (check_final=False) still see everything.
    """

    @pytest.fixture
    def suspense_tournament(self, completed_tournament):
        # Earlier rounds are published; the *final* round is fully scored but has
        # no PublishedRound row yet — the exact pre-ceremony state where the leak
        # used to occur. Spike one final-round seat + one self-draw hand to the
        # unique overall best so a leak would be unmistakable.
        nb_rounds = completed_tournament['variable'].nb_rounds
        tenant = completed_tournament['tenant']
        PublishedRound.objects.filter(tenant=tenant, round_nb=nb_rounds).delete()
        pos = Position.objects.filter(tenant=tenant, round_nb=nb_rounds).first()
        pos.minipoints = 100000
        pos.save()
        h = Hand.objects.filter(tenant=tenant, round_nb=nb_rounds, hand_nb=1).first()
        h.pts, h.win_by, h.win_from = 100000, 1, 0  # self-draw
        h.save()
        return completed_tournament

    def test_public_overall_excludes_unpublished_final_round(self, request_, suspense_tournament):
        nb_rounds = suspense_tournament['variable'].nb_rounds
        overall = views.stat_all_rounds(request_, check_final=True)
        # Roll-up still has earlier-round data, but nothing from the withheld final.
        assert overall['mp_max']
        assert all(p.round_nb < nb_rounds for p in overall['mp_max'])
        assert all(h['pts'] < 100000 for h in overall['sd_hand_max'])

    def test_admin_overall_includes_final_round(self, request_, suspense_tournament):
        nb_rounds = suspense_tournament['variable'].nb_rounds
        overall = views.stat_all_rounds(request_, check_final=False)
        assert any(
            p.round_nb == nb_rounds and p.minipoints == 100000 for p in overall['mp_max']
        )
        assert any(h['pts'] == 100000 for h in overall['sd_hand_max'])

    def test_default_is_unmasked_for_ceremony(self, request_, suspense_tournament):
        # ceremony.py calls stat_all_rounds(request) with no check_final and must
        # keep seeing the final round (the reveal surface).
        nb_rounds = suspense_tournament['variable'].nb_rounds
        overall = views.stat_all_rounds(request_)
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
        nb_rounds = completed_tournament['variable'].nb_rounds
        rounds = views.stat_rounds(request_, check_final=True)
        # Not [] — rounds 1..n-1 are shown, matching the standings/modal cutoff.
        assert len(rounds) == nb_rounds - 1
        assert len(rounds) == public_round_max(
            completed_tournament['tenant'], completed_tournament['variable'], force_all=False
        )

    def test_public_overall_cards_exclude_final_but_are_not_empty(self, request_, completed_tournament):
        nb_rounds = completed_tournament['variable'].nb_rounds
        overall = views.stat_all_rounds(request_, check_final=True)
        assert overall['mp_max']
        assert all(p.round_nb < nb_rounds for p in overall['mp_max'])

    def test_admin_still_sees_the_final_round(self, request_, completed_tournament):
        nb_rounds = completed_tournament['variable'].nb_rounds
        rounds = views.stat_rounds(request_, check_final=False)
        assert len(rounds) == nb_rounds


class TestNoRoundsFallback:
    """Before any round is scored, the standings would read "Scores after round 0"
    over an all-zero table — meaningless, so the view falls back to the schedule."""

    @pytest.fixture
    def unscored_tournament(self, tournament):
        # Wipe every scored round: no hands, and Positions carry no points.
        Hand.objects.filter(tenant=tournament['tenant']).delete()
        Position.objects.filter(tenant=tournament['tenant']).update(
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
        expected = segno.make('https://test.mahj.ovh', error='m').svg_inline(scale=3, border=2)
        assert svg == expected

    def test_qr_helper_empty_without_subdomain(self):
        from mahj.views.display import _spectator_qr_svg
        assert _spectator_qr_svg('') == ''
