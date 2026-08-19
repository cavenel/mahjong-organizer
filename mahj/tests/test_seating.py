"""Seating generator (mahj.seating).

Pure-Python, no DB — verifies the guarantees the generator promises: everyone
seated once per round, no teammate ever shares a table (team events), no rematch
where the algebraic construction applies, and honest refusal where a clean team
chart is impossible.
"""
from collections import Counter
from itertools import combinations

import pytest

from mahj import seating as S


def _tables(rows):
    """{(round, table): [draw_numbers]} from generator rows."""
    out = {}
    for (r, t, w, d) in rows:
        out.setdefault((r, t), []).append(d)
    return out


def _assert_complete(rows, N, R):
    tables = _tables(rows)
    for r in range(1, R + 1):
        seated = [d for (rr, t), ds in tables.items() if rr == r for d in ds]
        assert sorted(seated) == list(range(1, N + 1)), f"round {r} not a full seating"
        for (rr, t), ds in tables.items():
            if rr == r:
                assert len(ds) == 4
                winds = [w for (r2, t2, w, d) in rows if r2 == rr and t2 == t]
                assert sorted(winds) == [1, 2, 3, 4], "each table must have E/S/W/N once"


def _max_meetings(rows):
    meet = Counter()
    for ds in _tables(rows).values():
        for a, b in combinations(sorted(ds), 2):
            meet[(a, b)] += 1
    return max(meet.values()) if meet else 0


# --- algebraic: the OEMC regime -------------------------------------------

@pytest.mark.parametrize("N", [152, 156, 160, 164, 168, 100, 200])
def test_algebraic_team_chart_is_optimal(N):
    rows, meta = S.generate(N, 11, has_teams=True)
    assert meta["engine"] == "algebraic"
    _assert_complete(rows, N, 11)
    m = S.measure(rows, N, 11, has_teams=True)
    assert m["teammate_clashes"] == 0      # teammates never share a table
    assert m["max_meetings"] == 1          # no rematch
    assert m["east_min"] >= m["east_ideal_low"] and m["east_max"] <= m["east_ideal_high"]
    assert m["table_spread_min"] == m["table_spread_ideal"]  # 11 distinct tables


def test_algebraic_matches_oemc_published_shift():
    """The 168-player SHIFT set the generator picks must be a valid rematch-free
    set (11 distinct, no degenerate tables) — the property the OEMC PDF relies on."""
    shifts = S.find_shifts(42, 11, seed=0)
    assert len(shifts) == 11
    forb = S._forbidden_shifts(42)
    assert not (set(shifts) & forb)
    for mod in S._shift_moduli(42):
        assert len({s % mod for s in shifts}) == 11  # pairwise distinct per modulus


# --- individual events, including the small/awkward fields -----------------

@pytest.mark.parametrize("N,R", [(48, 7), (72, 7), (72, 11), (120, 11)])
def test_individual_reaches_no_rematch_when_feasible(N, R):
    rows, meta = S.generate(N, R, has_teams=False)
    _assert_complete(rows, N, R)
    assert _max_meetings(rows) == 1  # feasible field sizes come out rematch-free


@pytest.mark.parametrize("N,R", [(8, 11), (16, 7), (24, 11)])
def test_small_fields_stay_valid_even_when_rematches_forced(N, R):
    """Below the (N-1)/3 ceiling rematches are unavoidable; the chart must still
    be complete and honestly reported, not crash or drop players."""
    rows, meta = S.generate(N, R, has_teams=False)
    _assert_complete(rows, N, R)
    m = S.measure(rows, N, R, has_teams=False)
    assert m["all_seated"]
    assert m["max_meetings"] >= 2  # forced
    assert "Best achievable" in S.headline(m)


# --- refusals / edge cases -------------------------------------------------

def test_team_algebraic_refuses_when_infeasible():
    """Forcing the deterministic method where no clean team chart exists refuses,
    rather than returning one that seats teammates together."""
    with pytest.raises(S.SeatingInfeasible):
        S.generate(24, 11, has_teams=True, method="algebraic")


def test_team_auto_falls_back_to_best_effort():
    """The default 'auto' still produces a (best-effort) chart for an infeasible
    team field, minimising teammate clashes rather than refusing."""
    rows, meta = S.generate(24, 11, has_teams=True)  # method='auto'
    assert meta["engine"] == "greedy"
    m = S.measure(rows, 24, 11, has_teams=True)
    assert m["all_seated"]


def test_small_team_field_clashes_are_minimised():
    """With fewer than 4 teams a clash at every table is unavoidable; the chart is
    still complete and hits that theoretical minimum (one clash per table)."""
    rows, meta = S.generate(12, 11, has_teams=True)  # 3 teams -> forced clashes
    m = S.measure(rows, 12, 11, has_teams=True)
    assert m["all_seated"]
    assert m["teammate_clashes"] == 3 * 11  # 3 tables x 11 rounds, one forced each


@pytest.mark.parametrize("N", [0, 4, 7, 18, 6])
def test_invalid_player_counts_rejected(N):
    with pytest.raises(S.SeatingInfeasible):
        S.generate(N, 7, has_teams=False)


def test_zero_rounds_rejected():
    with pytest.raises(S.SeatingInfeasible):
        S.generate(16, 0, has_teams=False)


def test_generation_is_deterministic():
    """Same inputs -> same chart, so a generated tournament is reproducible."""
    a, _ = S.generate(168, 11, has_teams=True)
    b, _ = S.generate(168, 11, has_teams=True)
    assert a == b


class TestFeasibilityBadgeMatchesTheGenerator:
    """`algebraic_feasible` drives a UI badge that tells the organizer a
    rematch-free chart is impossible. It must never say that when `generate` would
    in fact succeed."""

    # A representative spread rather than every size: the full search runs 300
    # orderings, so exhausting the grid costs the suite ~30s for no more signal.
    FIELDS = (8, 12, 16, 20, 24, 32, 40, 48, 64)
    ROUNDS = range(1, 11)

    def test_badge_agrees_with_the_full_search(self):
        """The badge runs find_shifts' first (ascending) pass only. The full search
        runs that same pass as its first trial, so it can only do as well or better
        — and across the plausible field sizes they come out identical."""
        for nb_players in self.FIELDS:
            tables = nb_players // 4
            for nb_rounds in self.ROUNDS:
                badge = S.algebraic_feasible(nb_players, nb_rounds)
                full = len(S.find_shifts(tables, nb_rounds, seed=0)) >= nb_rounds
                assert badge == full, (
                    f'badge={badge} but the full search says {full} '
                    f'for {nb_players} players over {nb_rounds} rounds')

    def test_badge_is_never_optimistic(self):
        """The direction that matters: a True must be backed by a real shift set."""
        for nb_players in self.FIELDS:
            for nb_rounds in self.ROUNDS:
                if S.algebraic_feasible(nb_players, nb_rounds):
                    shifts = S.find_shifts(nb_players // 4, nb_rounds, seed=0)
                    assert len(shifts) >= nb_rounds

    def test_impossible_shapes_are_refused(self):
        assert not S.algebraic_feasible(4, 1)      # too small
        assert not S.algebraic_feasible(18, 1)     # not a multiple of 4
        assert not S.algebraic_feasible(16, 0)     # no rounds


class TestMeasureHandlesAnImportedChart:
    """measure() used to build fixed-size lists indexed by draw number and round, so
    a chart that doesn't seat exactly slots 1..N over rounds 1..R raised
    IndexError/KeyError — and the seating page's blanket except then hid the whole
    quality panel with no clue why."""

    def _rows(self, groups):
        """groups: {round: [[draw x4], ...]} -> (round, table, wind, draw) rows."""
        rows = []
        for round_nb, tables in groups.items():
            for table_nb, draws in enumerate(tables, start=1):
                for wind, draw in enumerate(draws, start=1):
                    rows.append((round_nb, table_nb, wind, draw))
        return rows

    def test_draw_numbers_above_n_do_not_raise(self):
        """A chart seating slots 5..12 for an 8-player field: every number is beyond
        the old list's length."""
        rows = self._rows({1: [[5, 6, 7, 8], [9, 10, 11, 12]],
                           2: [[5, 7, 9, 11], [6, 8, 10, 12]]})
        m = S.measure(rows, 8, 2)
        assert m['N'] == 8 and m['R'] == 2
        assert m['east_min'] >= 0
        assert m['table_spread_min'] >= 1

    def test_sparse_draw_numbers_do_not_raise(self):
        rows = self._rows({1: [[1, 2, 5, 9]], 2: [[1, 5, 2, 9]]})
        m = S.measure(rows, 4, 2)
        assert m['max_meetings'] >= 1

    def test_more_rounds_than_asked_for_do_not_raise(self):
        rows = self._rows({1: [[1, 2, 3, 4]], 2: [[1, 2, 3, 4]], 7: [[1, 2, 3, 4]]})
        m = S.measure(rows, 4, 2)
        # The chart covers more rounds than requested, so it isn't what was asked for.
        assert m['all_seated'] is False

    def test_a_complete_chart_still_reads_as_complete(self):
        rows = self._rows({1: [[1, 2, 3, 4], [5, 6, 7, 8]],
                           2: [[1, 3, 5, 7], [2, 4, 6, 8]]})
        m = S.measure(rows, 8, 2)
        assert m['all_seated'] is True

    def test_a_missing_seat_reads_as_incomplete(self):
        rows = self._rows({1: [[1, 2, 3, 4], [5, 6, 7, 8]],
                           2: [[1, 3, 5, 7], [2, 4, 6, 8]]})
        m = S.measure(rows[:-1], 8, 2)
        assert m['all_seated'] is False

    def test_generated_charts_are_unaffected(self):
        """The normal path must give the same verdict as before."""
        rows, _meta = S.generate(16, 3, seed=0)
        m = S.measure(rows, 16, 3)
        assert m['all_seated'] is True
        assert m['max_meetings'] == 1
