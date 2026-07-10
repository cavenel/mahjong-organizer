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
