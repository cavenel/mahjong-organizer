"""Pure-Python unit tests for helpers in mahj/scoring.py — no DB."""
from types import SimpleNamespace

from mahj.scoring import (
    _assign_ranks,
    _country_flag,
    _group_by,
    _roll_up,
    _top_by,
    _top_win_streaks,
    team_standings,
)


def _player_row(team, tp, mp, pid):
    """Minimal player standing row in the shape team_standings consumes."""
    return {
        'team': team, 'flag': '', 'player_id': pid,
        'total': {'tp': tp, 'mp': mp},
        'scores': [{'tp': tp, 'mp': mp, 'round_nb': 1}],
    }


class TestTeamStandings:
    def test_tied_teams_share_a_position(self):
        # Teams B and C are level on both TP and MP — they must share pos 2,
        # exactly like tied players, and the next team drops to pos 4.
        rows = [
            _player_row('A', tp=30.0, mp=300, pid=1),
            _player_row('B', tp=20.0, mp=200, pid=2),
            _player_row('C', tp=20.0, mp=200, pid=3),
            _player_row('D', tp=10.0, mp=100, pid=4),
        ]
        tournament = SimpleNamespace(rules='MCR', nb_rounds=1)
        by_team = {t['team']: t for t in team_standings(rows, tournament, 1)}
        assert by_team['A']['pos'] == 1
        assert by_team['B']['pos'] == by_team['C']['pos'] == 2
        assert by_team['D']['pos'] == 4

    def test_same_tp_different_mp_are_not_tied(self):
        # MCR ranks on TP, but a tie needs both TP and MP equal; differing MP
        # breaks the tie into distinct positions.
        rows = [
            _player_row('A', tp=20.0, mp=250, pid=1),
            _player_row('B', tp=20.0, mp=200, pid=2),
        ]
        tournament = SimpleNamespace(rules='MCR', nb_rounds=1)
        by_team = {t['team']: t for t in team_standings(rows, tournament, 1)}
        assert by_team['A']['pos'] == 1
        assert by_team['B']['pos'] == 2

    def test_riichi_same_mp_different_tp_are_tied(self):
        # Counterpart to test_same_tp_different_mp_are_not_tied: Riichi ranks on
        # MP alone, so equal MP ties even when TP differs (TP isn't part of the
        # order at all).
        rows = [
            _player_row('A', tp=20.0, mp=200, pid=1),
            _player_row('B', tp=5.0, mp=200, pid=2),
        ]
        tournament = SimpleNamespace(rules='Riichi', nb_rounds=1)
        by_team = {t['team']: t for t in team_standings(rows, tournament, 1)}
        assert by_team['A']['pos'] == by_team['B']['pos'] == 1

    def test_per_round_folds_by_round_not_list_position(self):
        # F-M5: a player's per-round scores are folded into the team's column for
        # the score's actual round_nb, not its position in the (compact) list. Here
        # player 2 missed round 1, so their only score is round 2 — at list index 0.
        # Keying on position would drop that round-2 score into the team's round-1
        # column; keying on round_nb puts it in round 2.
        rows = [
            {'team': 'X', 'flag': '', 'player_id': 1,
             'total': {'tp': 7.0, 'mp': 300},
             'scores': [{'tp': 4.0, 'mp': 100, 'round_nb': 1},
                        {'tp': 3.0, 'mp': 200, 'round_nb': 2}]},
            {'team': 'X', 'flag': '', 'player_id': 2,
             'total': {'tp': 1.0, 'mp': 50},
             'scores': [{'tp': 1.0, 'mp': 50, 'round_nb': 2}]},
        ]
        tournament = SimpleNamespace(rules='MCR', nb_rounds=2)
        team = team_standings(rows, tournament, 2)[0]
        r1, r2 = team['scores']
        assert (r1['round_nb'], r1['mp'], r1['tp']) == (1, 100, 4.0)
        assert (r2['round_nb'], r2['mp'], r2['tp']) == (2, 250, 4.0)

    def test_non_mcr_ties_on_mp_alone_ignoring_tp(self):
        # Non-MCR (e.g. Riichi) ranks on MP only. Teams level on MP share a
        # position even if their (display-only) TP differs; TP must not split
        # the tie. Input order is deliberately tp-shuffled to catch the
        # order-dependent grouping bug.
        rows = [
            _player_row('A', tp=9.0, mp=300, pid=1),
            _player_row('B', tp=5.0, mp=200, pid=2),
            _player_row('C', tp=3.0, mp=200, pid=3),
            _player_row('D', tp=5.0, mp=200, pid=4),
            _player_row('E', tp=1.0, mp=100, pid=5),
        ]
        tournament = SimpleNamespace(rules='Riichi', nb_rounds=1)
        by_team = {t['team']: t for t in team_standings(rows, tournament, 1)}
        assert by_team['A']['pos'] == 1
        assert by_team['B']['pos'] == by_team['C']['pos'] == by_team['D']['pos'] == 2
        assert by_team['E']['pos'] == 5


class TestAssignRanks:
    def test_all_unique(self):
        rows = [{'v': 30}, {'v': 20}, {'v': 10}]
        _assign_ranks(rows, key=lambda r: r['v'], field='pos')
        assert [r['pos'] for r in rows] == [1, 2, 3]

    def test_shared_rank_skips_next(self):
        rows = [{'v': 30}, {'v': 20}, {'v': 20}, {'v': 10}]
        _assign_ranks(rows, key=lambda r: r['v'], field='pos')
        assert [r['pos'] for r in rows] == [1, 2, 2, 4]

    def test_all_tied(self):
        rows = [{'v': 1}, {'v': 1}, {'v': 1}]
        _assign_ranks(rows, key=lambda r: r['v'], field='pos')
        assert [r['pos'] for r in rows] == [1, 1, 1]

    def test_empty(self):
        rows = []
        _assign_ranks(rows, key=lambda r: r['v'], field='pos')
        assert rows == []


class TestTopBy:
    def test_empty(self):
        assert _top_by([], key=lambda x: x) == []

    def test_single_max(self):
        items = [SimpleNamespace(v=1), SimpleNamespace(v=5), SimpleNamespace(v=3)]
        top = _top_by(items, key=lambda x: x.v)
        assert [x.v for x in top] == [5]

    def test_ties_at_max(self):
        items = [SimpleNamespace(v=5), SimpleNamespace(v=3), SimpleNamespace(v=5)]
        top = _top_by(items, key=lambda x: x.v)
        assert [x.v for x in top] == [5, 5]

    def test_exclude_zero_when_max_is_zero(self):
        items = [SimpleNamespace(v=0), SimpleNamespace(v=0)]
        assert _top_by(items, key=lambda x: x.v, exclude_zero=True) == []

    def test_exclude_zero_when_max_is_positive(self):
        items = [SimpleNamespace(v=0), SimpleNamespace(v=7)]
        top = _top_by(items, key=lambda x: x.v, exclude_zero=True)
        assert [x.v for x in top] == [7]


class TestGroupBy:
    def test_groups_by_key(self):
        items = [('a', 1), ('b', 2), ('a', 3)]
        out = _group_by(items, key=lambda t: t[0])
        assert out['a'] == [('a', 1), ('a', 3)]
        assert out['b'] == [('b', 2)]

    def test_missing_key_returns_empty_list(self):
        out = _group_by([], key=lambda t: t)
        assert out['missing'] == []


class TestRollUp:
    def test_picks_single_best_round(self):
        rounds = [
            {'mp': [{'v': 10}]},
            {'mp': [{'v': 50}]},
            {'mp': [{'v': 20}]},
        ]
        best = _roll_up(rounds, 'mp', value_of=lambda d: d['v'])
        assert best == [{'v': 50}]

    def test_merges_tied_rounds(self):
        rounds = [
            {'mp': [{'id': 'r1'}]},
            {'mp': [{'id': 'r2'}]},
            {'mp': [{'id': 'r3'}]},
        ]
        best = _roll_up(rounds, 'mp', value_of=lambda d: 100)
        assert best == [{'id': 'r1'}, {'id': 'r2'}, {'id': 'r3'}]

    def test_skips_empty_rounds(self):
        rounds = [{'mp': []}, {'mp': [{'v': 5}]}, {}]
        best = _roll_up(rounds, 'mp', value_of=lambda d: d['v'])
        assert best == [{'v': 5}]

    def test_all_empty(self):
        assert _roll_up([{'mp': []}, {}], 'mp', value_of=lambda d: d['v']) == []

    def test_all_negative_tops_still_pick_the_best(self):
        # Minipoints are zero-sum and routinely negative; an all-negative field
        # must still roll up the least-negative round (a 0 seed dropped it).
        rounds = [
            {'mp': [{'v': -30}]},
            {'mp': [{'v': -5}]},
            {'mp': [{'v': -20}]},
        ]
        assert _roll_up(rounds, 'mp', value_of=lambda d: d['v']) == [{'v': -5}]

    def test_zero_top_is_picked_not_treated_as_absent(self):
        rounds = [{'mp': [{'v': -10}]}, {'mp': [{'v': 0}]}]
        assert _roll_up(rounds, 'mp', value_of=lambda d: d['v']) == [{'v': 0}]


class TestCountryFlag:
    def test_known(self):
        assert _country_flag('Sweden') == 'se'
        assert _country_flag('France') == 'fr'
        assert _country_flag('Japan') == 'jp'

    def test_strips_leading_the(self):
        assert _country_flag('The Netherlands') == 'nl'

    def test_unknown_returns_empty(self):
        assert _country_flag('Atlantis') == ''

    def test_empty_or_junk(self):
        assert _country_flag('') == ''
        assert _country_flag('  ') == ''

    def test_aliased_names_pycountry_gets_wrong(self):
        # "Turkey" misses pycountry entirely (renamed "Türkiye"); bare "Korea"
        # fuzzy-matches North Korea (kp) instead of South Korea (kr).
        assert _country_flag('Turkey') == 'tr'
        assert _country_flag('Türkiye') == 'tr'
        assert _country_flag('Korea') == 'kr'
        assert _country_flag('South Korea') == 'kr'


class TestTopWinStreaks:
    def test_no_hands(self):
        assert _top_win_streaks([]) == []

    def test_no_valid_seat_winners(self):
        # win_by=0 means "no winner" — function only groups seats 1..4, so these produce no streaks.
        hands = [SimpleNamespace(table_nb=1, win_by=0, hand_nb=i, pts=0, win_by_player='') for i in range(3)]
        assert _top_win_streaks(hands) == []

    def test_picks_max_group(self):
        hands = [
            SimpleNamespace(round_nb=1, table_nb=1, win_by=1, win_by_player='P1', hand_nb=i, pts=10)
            for i in range(5)
        ] + [
            SimpleNamespace(round_nb=1, table_nb=1, win_by=2, win_by_player='P2', hand_nb=i, pts=10)
            for i in range(2)
        ]
        result = _top_win_streaks(hands)
        assert len(result) == 1
        assert result[0]['nb_win'] == 5
        assert result[0]['player'] == 'P1'

    def test_keeps_all_ties(self):
        hands = [
            SimpleNamespace(round_nb=1, table_nb=1, win_by=1, win_by_player='P1', hand_nb=i, pts=10) for i in range(3)
        ] + [
            SimpleNamespace(round_nb=1, table_nb=2, win_by=3, win_by_player='P3', hand_nb=i, pts=10) for i in range(3)
        ]
        result = _top_win_streaks(hands)
        assert len(result) == 2
        assert {r['player'] for r in result} == {'P1', 'P3'}
