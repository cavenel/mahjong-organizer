"""Pure-Python unit tests for helpers in SOMMC2018/scoring.py — no DB."""
from types import SimpleNamespace

from SOMMC2018.scoring import (
    _assign_ranks,
    _country_flag,
    _group_by,
    _roll_up,
    _top_by,
    _top_win_streaks,
)


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


class TestTopWinStreaks:
    def test_no_hands(self):
        assert _top_win_streaks([]) == []

    def test_no_valid_seat_winners(self):
        # win_by=0 means "no winner" — function only groups seats 1..4, so these produce no streaks.
        hands = [SimpleNamespace(table_nb=1, win_by=0, hand_nb=i, pts=0, win_by_player='') for i in range(3)]
        assert _top_win_streaks(hands) == []

    def test_picks_max_group(self):
        hands = [
            SimpleNamespace(table_nb=1, win_by=1, win_by_player='P1', hand_nb=i, pts=10)
            for i in range(5)
        ] + [
            SimpleNamespace(table_nb=1, win_by=2, win_by_player='P2', hand_nb=i, pts=10)
            for i in range(2)
        ]
        result = _top_win_streaks(hands)
        assert len(result) == 1
        assert result[0]['nb_win'] == 5
        assert result[0]['player'] == 'P1'

    def test_keeps_all_ties(self):
        hands = [
            SimpleNamespace(table_nb=1, win_by=1, win_by_player='P1', hand_nb=i, pts=10) for i in range(3)
        ] + [
            SimpleNamespace(table_nb=2, win_by=3, win_by_player='P3', hand_nb=i, pts=10) for i in range(3)
        ]
        result = _top_win_streaks(hands)
        assert len(result) == 2
        assert {r['player'] for r in result} == {'P1', 'P3'}
