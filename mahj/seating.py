"""Tournament seating generation.

A seating chart in this app is a set of seats keyed by ``draw_number`` (1..N):
each seat is ``(round_nb, table_nb, wind, draw_number)`` and is independent of the
people (a Player attaches to a draw number later). This module builds such charts
for any field size, so importing one from a workbook is optional.

Two engines, picked automatically by :func:`generate`:

* **Algebraic** — the deterministic, publicly verifiable construction (round r,
  team ``k`` = ``(d-1)//4``, role ``m`` = ``(d-1)%4``)::

      table = ((k + m*SHIFT[r] + OFFSET[r]) mod T) + 1
      wind  = ((m + WIND_OFFSET[r]) mod 4) + 1      # 1=East .. 4=North

  Teammates (a consecutive quartet of draw numbers) never share a table, and no
  two players meet twice — both automatic when the SHIFT values satisfy the
  validity conditions below. Only usable when R such SHIFTs exist for T teams.

* **Greedy** — a best-effort fallback for small/awkward fields where the
  algebraic no-repeat guarantee is mathematically impossible (e.g. 8 players over
  7 rounds). It minimises opponent repeats and still avoids teammates when it can.

All winds use 1=East, 2=South, 3=West, 4=North to match ``Seat.wind``.
"""
import random
import time
from collections import Counter
from itertools import combinations
from math import gcd

WINDS = 4


class SeatingInfeasible(Exception):
    """Raised when the requested chart cannot be built with the chosen engine."""


# --- algebraic engine ------------------------------------------------------

def _forbidden_shifts(T):
    """SHIFT values that would make a table degenerate: ``m*s ≡ 0 (mod T)`` for
    some role difference ``m`` in {1,2,3} puts two roles of one team on the same
    table (teammates meet) or collapses a table. Includes 0."""
    forb = set()
    for m in (1, 2, 3):
        period = T // gcd(m, T)
        for k in range(0, T, period):
            forb.add(k % T)
    return forb


def _shift_moduli(T):
    """Two seats collide in round r iff ``SHIFT[r]*dm ≡ dk (mod T)`` for their role
    gap ``dm`` and team gap ``dk``. No pair meets twice iff the SHIFTs are pairwise
    distinct modulo ``T/gcd(dm,T)`` for every ``dm`` in {1,2,3}."""
    return sorted({T // gcd(m, T) for m in (1, 2, 3)})


def find_shifts(T, R, seed=0, trials=300):
    """Up to ``R`` SHIFT values valid for the no-repeat guarantee. Returns as many
    as it can find (``< R`` means the guarantee is infeasible for this T/R). When
    several full sets exist, prefers the one giving the tightest team-vs-team
    spread, then the numerically smallest, so the result is stable."""
    forb = _forbidden_shifts(T)
    cands = [s for s in range(1, T) if s not in forb]
    moduli = _shift_moduli(T)
    rng = random.Random(seed)

    def valid_subset(order):
        chosen, used = [], [set() for _ in moduli]
        for s in order:
            res = [s % m for m in moduli]
            if all(res[i] not in used[i] for i in range(len(moduli))):
                chosen.append(s)
                for i, r in enumerate(res):
                    used[i].add(r)
                if len(chosen) == R:
                    break
        return chosen

    best_partial = []
    best_full = None
    best_score = None
    for trial in range(trials):
        order = sorted(cands) if trial == 0 else rng.sample(cands, len(cands))
        chosen = valid_subset(order)
        if len(chosen) > len(best_partial):
            best_partial = chosen
        if len(chosen) == R:
            score = (_team_pair_spread(T, sorted(chosen)), tuple(sorted(chosen)))
            if best_score is None or score < best_score:
                best_score = score
                best_full = sorted(chosen)
    if best_full is not None:
        return best_full
    return sorted(best_partial)


def _team_pair_spread(T, shifts):
    """Spread (max-min) of how often team pairs meet, over the whole tournament,
    for a candidate SHIFT set. Lower is fairer. Cheap enough to rank candidates."""
    meet = Counter()
    for s in shifts:
        # In one round team k and team k' share a table iff k-k' ≡ dm*s (mod T)
        # for some role gap dm in {1,2,3} (and its negative). Count unordered pairs.
        for dm in (1, 2, 3):
            d = (dm * s) % T
            for k in range(T):
                a, b = k, (k + d) % T
                meet[(min(a, b), max(a, b))] += 1
    if not meet:
        return 0
    vals = list(meet.values())
    return max(vals) - min(vals)


def find_offsets(T, R, shifts, seed=0, attempts=3000):
    """Per-round table offsets so each role visits as many distinct tables as
    possible (ideally ``min(R, T)``): keeps players moving around the room instead
    of role-0 sitting at the same table every round. Built greedily round by
    round — each offset is chosen to avoid sending any role back to a table it has
    already used — with randomised retries to escape a dead end."""
    rng = random.Random(seed + 1)
    target = min(R, T)
    best, best_score = [0] * R, -1
    for _ in range(attempts):
        offs, seen = [], [set() for _ in range(4)]
        for r in range(R):
            # An offset o sends role m to table (m*SHIFT[r]+o) mod T; forbid the o
            # that repeats any role's earlier table.
            forbidden = {(v - m * shifts[r]) % T for m in range(4) for v in seen[m]}
            choices = [o for o in range(T) if o not in forbidden]
            o = rng.choice(choices) if choices else rng.randrange(T)
            offs.append(o)
            for m in range(4):
                seen[m].add((m * shifts[r] + o) % T)
        score = min(len(s) for s in seen)
        if score > best_score:
            best, best_score = offs, score
            if best_score >= target:
                break
    return best


def wind_offsets(R, seed=0, trials=2000):
    """A per-round wind rotation whose multiset is as balanced as possible (each
    wind ``R//4`` or ``R//4+1`` times) and, where possible, has no two consecutive
    rounds with the same rotation — so wind sequences don't come in blocks."""
    counts = [R // 4 + (1 if i < R % 4 else 0) for i in range(4)]
    multiset = []
    for w, c in enumerate(counts):
        multiset += [w] * c
    rng = random.Random(seed + 2)
    for _ in range(trials):
        rng.shuffle(multiset)
        if all(multiset[i] != multiset[i - 1] for i in range(1, len(multiset))):
            break
    return multiset


def generate_algebraic(N, R, seed=0):
    """Build the chart with the algebraic construction, or raise
    :class:`SeatingInfeasible` if R valid SHIFTs don't exist for this field."""
    if N % 4 != 0:
        raise SeatingInfeasible("The number of players must be a multiple of 4.")
    T = N // 4
    shifts = find_shifts(T, R, seed)
    if len(shifts) < R:
        raise SeatingInfeasible(
            f"No rematch-free chart exists for {N} players over {R} rounds "
            f"(at most {len(shifts)} such rounds are possible)."
        )
    offs = find_offsets(T, R, shifts, seed)
    wo = wind_offsets(R, seed)
    rows = []
    for d in range(1, N + 1):
        k, m = (d - 1) // 4, (d - 1) % 4
        for r in range(R):
            table = ((k + m * shifts[r] + offs[r]) % T) + 1
            wind = ((m + wo[r]) % 4) + 1
            rows.append((r + 1, table, wind, d))
    params = {"shift": shifts, "offset": offs, "wind_offset": wo}
    return rows, params


# --- greedy engine ---------------------------------------------------------

def _team_of(d):
    """Consecutive-quartet team of a draw number (draw 1..4 -> team 0)."""
    return (d - 1) // 4


class _Timeout(Exception):
    pass


def _partition_no_repeat(N, met, use_teams, rng, node_budget):
    """Backtracking partition of 1..N into tables of four with **no** already-met
    pair repeated and (when ``use_teams``) four distinct teams per table. Returns
    the groups, or None if it can't within ``node_budget`` search nodes. Does not
    mutate ``met`` — the caller folds the returned groups in on success. The budget
    is a node count (not wall-clock) so a given rng seed always yields the same
    result, which keeps whole charts reproducible from their seed."""
    used = set(met)
    remaining = set(range(1, N + 1))
    groups = []
    nodes = [0]

    def bt():
        nodes[0] += 1
        if nodes[0] > node_budget:
            raise _Timeout
        if not remaining:
            return True
        p = min(remaining)  # fixing the smallest cuts the search symmetrically
        base = [q for q in remaining if q != p
                and (p, q) not in used  # p == min(remaining) < q, already canonical
                and (not use_teams or _team_of(q) != _team_of(p))]
        rng.shuffle(base)
        for a, b, c in combinations(base, 3):
            grp = (p, a, b, c)
            if use_teams and len({_team_of(x) for x in grp}) < 4:
                continue
            pairs = [(min(x, y), max(x, y)) for x, y in combinations(grp, 2)]
            if any(pr in used for pr in pairs):
                continue
            used.update(pairs)
            remaining.difference_update(grp)
            groups.append(grp)
            if bt():
                return True
            groups.pop()
            remaining.update(grp)
            used.difference_update(pairs)
        return False

    try:
        return groups if bt() else None
    except _Timeout:
        return None


def _build_round_relaxed(N, met, use_teams, rng):
    """Last-resort round builder that always completes, allowing repeats (and
    relaxing teams) only when nothing else fits — used to fill rounds the strict
    backtracker couldn't place without a rematch."""
    remaining = set(range(1, N + 1))
    groups = []
    while remaining:
        seed_p = rng.choice(tuple(remaining))
        remaining.discard(seed_p)
        grp = [seed_p]
        while len(grp) < 4:
            cand = list(remaining)
            if use_teams:
                team_ok = [q for q in cand if all(_team_of(q) != _team_of(x) for x in grp)]
                if team_ok:
                    cand = team_ok
            cand.sort(key=lambda q: (sum(((min(q, x), max(q, x))) in met for x in grp),
                                     rng.random()))
            pick = cand[0]
            remaining.discard(pick)
            grp.append(pick)
        for a, b in combinations(grp, 2):
            met.add((min(a, b), max(a, b)))
        groups.append(tuple(grp))
    return groups


def _assign_winds(groups_by_round, N, R, rng):
    """Choose a wind for each seat so every player's wind counts stay close to the
    balanced profile. Greedy per table against running per-player counts, with a
    few local-search passes."""
    wc = [[0, 0, 0, 0] for _ in range(N + 1)]
    target = R / 4.0

    def dev(p):
        return sum((c - target) ** 2 for c in wc[p])

    assign = []  # assign[r][t] = [wind0..3] for the 4 players of that table
    for groups in groups_by_round:
        round_assign = []
        for grp in groups:
            best_perm, best_cost = None, None
            perms = list(_PERMS)
            rng.shuffle(perms)
            for perm in perms:
                cost = 0
                for i, p in enumerate(grp):
                    wc[p][perm[i]] += 1
                cost = sum(dev(p) for p in grp)
                for i, p in enumerate(grp):
                    wc[p][perm[i]] -= 1
                if best_cost is None or cost < best_cost:
                    best_cost, best_perm = cost, perm
            for i, p in enumerate(grp):
                wc[p][best_perm[i]] += 1
            round_assign.append(list(best_perm))
        assign.append(round_assign)
    return assign


_PERMS = [
    (a, b, c, d)
    for a in range(4) for b in range(4) for c in range(4) for d in range(4)
    if len({a, b, c, d}) == 4
]


def _greedy_once(N, R, use_teams, seed, node_budget=40000):
    """One deterministic best-effort chart for the given ``seed``. Builds each
    round with the no-repeat backtracker, filling any round it can't place without
    a rematch via the relaxed builder. Returns ``(rounds, key)`` where ``key`` is
    ``(teammate_clashes, opponent_repeats)`` — lower is better."""
    rng = random.Random((seed + 1) * 100003)
    met = set()
    rounds = []
    for _ in range(R):
        grp = _partition_no_repeat(N, met, use_teams, rng, node_budget)
        if grp is None:
            grp = _build_round_relaxed(N, met, use_teams, rng)
        else:
            for g in grp:
                for x, y in combinations(g, 2):
                    met.add((min(x, y), max(x, y)))
        rounds.append(grp)
    repeats, tm = _grouping_defects(rounds, use_teams)
    return rounds, (tm, repeats)


def _greedy_rows(rounds, N, R, seed):
    """Assign winds and physical tables to a set of groupings, deterministically
    from ``seed`` (so a chart reproduces exactly from its winning seed)."""
    rng = random.Random(seed + 7)
    winds = _assign_winds(rounds, N, R, rng)
    rows = []
    for r, groups in enumerate(rounds):
        # Randomise which physical table each group uses, so players move around
        # the room instead of a group always landing on table 1.
        table_order = list(range(len(groups)))
        rng.shuffle(table_order)
        for t, grp in enumerate(groups):
            for i, p in enumerate(grp):
                rows.append((r + 1, table_order[t] + 1, winds[r][t][i] + 1, p))
    return rows


def _greedy_score(rows, N, R, use_teams):
    """Lexicographic quality key for a candidate chart (lower is better): fewest
    teammate clashes, then rematches, then tightest wind (East) spread, then most
    distinct tables, then least wind blockiness. Matches the displayed measures'
    priority so extra tries improve the chart on every axis, not just rematches."""
    m = measure(rows, N, R, use_teams)
    return (
        (m["teammate_clashes"] or 0) if use_teams else 0,
        m["repeated_pairs"],
        m["east_max"] - m["east_min"],
        -m["table_spread_min"],
        m["max_consecutive_wind"],
    )


def generate_greedy(N, R, use_teams, seed=0, tries=1, budget=6.0):
    """Best-effort chart for fields where a rematch-free algebraic chart is
    impossible. Runs up to ``tries`` deterministic attempts (seeds ``seed``,
    ``seed+1``, …, capped by ``budget`` seconds) and keeps the best by
    :func:`_greedy_score`. The winning seed is returned in the metadata so the
    exact chart can be reproduced later with ``tries=1, seed=<that seed>``."""
    if N % 4 != 0:
        raise SeatingInfeasible("The number of players must be a multiple of 4.")
    tries = max(1, int(tries))
    deadline = time.monotonic() + budget
    best_rows, best_score, best_seed, ran = None, None, seed, 0
    for s in range(seed, seed + tries):
        rounds, _ = _greedy_once(N, R, use_teams, s)
        rows = _greedy_rows(rounds, N, R, s)
        score = _greedy_score(rows, N, R, use_teams)
        ran += 1
        if best_score is None or score < best_score:
            best_score, best_rows, best_seed = score, rows, s
        if time.monotonic() > deadline:
            break
    return best_rows, {"engine": "greedy", "seed": best_seed, "tries": tries, "tries_run": ran}


def _grouping_defects(groups_by_round, use_teams):
    """(opponent repeats, teammate clashes) for a set of rounds — the greedy
    objective, computed from the groupings before winds are assigned."""
    meet = Counter()
    tm = 0
    for groups in groups_by_round:
        for grp in groups:
            for a, b in combinations(grp, 2):
                meet[(min(a, b), max(a, b))] += 1
                if use_teams and _team_of(a) == _team_of(b):
                    tm += 1
    repeats = sum(c - 1 for c in meet.values() if c > 1)
    return repeats, tm


# --- public entry point ----------------------------------------------------

def algebraic_feasible(N, R):
    """Whether a rematch-free algebraic chart exists for this field/round count
    (cheap greedy check — no optimisation pass)."""
    if N < 8 or N % 4 != 0 or R < 1:
        return False
    T = N // 4
    forb = _forbidden_shifts(T)
    moduli = _shift_moduli(T)
    used = [set() for _ in moduli]
    count = 0
    for s in range(1, T):
        if s in forb:
            continue
        res = [s % m for m in moduli]
        if all(res[i] not in used[i] for i in range(len(moduli))):
            for i, r in enumerate(res):
                used[i].add(r)
            count += 1
            if count >= R:
                return True
    return False


def generate(N, R, has_teams=False, seed=0, method="auto", tries=50):
    """Build a seating chart for ``N`` players over ``R`` rounds.

    ``method`` selects the engine:

    * ``"auto"`` — algebraic when it yields a rematch-free chart, else best-effort.
    * ``"algebraic"`` — force the deterministic/verifiable construction; raises
      :class:`SeatingInfeasible` if it can't produce a rematch-free chart.
    * ``"greedy"`` — force the best-effort search: run ``tries`` attempts and keep
      the best (the winning seed is returned in ``meta`` for reproducibility).

    Returns ``(rows, meta)`` where ``rows`` are ``(round_nb, table_nb, wind,
    draw_number)`` tuples and ``meta`` records the engine and any parameters used.
    """
    if N < 8 or N % 4 != 0:
        raise SeatingInfeasible(
            "Seating can be generated for a multiple of 4 players, at least 8."
        )
    if R < 1:
        raise SeatingInfeasible("The tournament must have at least 1 round.")
    if method not in ("auto", "algebraic", "greedy"):
        method = "auto"

    if method in ("auto", "algebraic"):
        try:
            rows, params = generate_algebraic(N, R, seed)
            return rows, {"engine": "algebraic", **params}
        except SeatingInfeasible:
            if method == "algebraic":
                raise
            # "auto" falls back to the best-effort search. For team events it
            # can't promise teammates never meet (that guarantee needs the
            # algebraic chart, and is sometimes impossible anyway — e.g. fewer
            # than 4 teams forces a clash at every table), but it still minimises
            # teammate clashes then rematches, which beats offering nothing.
    return generate_greedy(N, R, has_teams, seed=seed, tries=tries)


# --- measures --------------------------------------------------------------

def measure(rows, N, R, has_teams=False):
    """Quality report for a chart. Reports worst-case / ranges (not averages), so
    an unfair chart can't hide behind a good mean."""
    by_round_table = {}
    east = Counter()
    wind_counts = [[0, 0, 0, 0] for _ in range(N + 1)]
    tables_of = {d: set() for d in range(1, N + 1)}
    wind_seq = {d: [None] * R for d in range(1, N + 1)}
    for (r, t, w, d) in rows:
        by_round_table.setdefault((r, t), []).append(d)
        wind_counts[d][w - 1] += 1
        if w == 1:
            east[d] += 1
        tables_of[d].add(t)
        wind_seq[d][r - 1] = w

    # Everyone seated exactly once per round?
    all_seated = True
    for r in range(1, R + 1):
        seated = [d for (rr, t), ds in by_round_table.items() if rr == r for d in ds]
        if sorted(seated) != list(range(1, N + 1)):
            all_seated = False
            break

    # Opponent meetings.
    meet = Counter()
    teammate_clashes = 0
    for ds in by_round_table.values():
        for a, b in combinations(sorted(ds), 2):
            meet[(a, b)] += 1
            if has_teams and _team_of(a) == _team_of(b):
                teammate_clashes += 1
    max_meet = max(meet.values()) if meet else 0
    repeated_pairs = sum(1 for c in meet.values() if c > 1)
    opp_count = Counter()
    for (a, b), c in meet.items():
        opp_count[a] += 1
        opp_count[b] += 1
    min_distinct_opp = min((opp_count[d] for d in range(1, N + 1)), default=0)

    east_vals = [east.get(d, 0) for d in range(1, N + 1)]
    max_consec = 0
    for d in range(1, N + 1):
        run = 1
        for i in range(1, R):
            if wind_seq[d][i] is not None and wind_seq[d][i] == wind_seq[d][i - 1]:
                run += 1
                max_consec = max(max_consec, run)
            else:
                run = 1
    max_consec = max(max_consec, 1 if R else 0)

    table_spread = min((len(tables_of[d]) for d in range(1, N + 1)), default=0)

    result = {
        "N": N, "R": R, "tables": N // 4, "engine": None,
        "all_seated": all_seated,
        "no_teammates": (teammate_clashes == 0) if has_teams else None,
        "teammate_clashes": teammate_clashes if has_teams else None,
        "max_meetings": max_meet,
        "repeated_pairs": repeated_pairs,
        "min_distinct_opponents": min_distinct_opp,
        "ideal_distinct_opponents": min(3 * R, N - 1),
        "east_min": min(east_vals) if east_vals else 0,
        "east_max": max(east_vals) if east_vals else 0,
        "east_ideal_low": R // 4,
        "east_ideal_high": (R + 3) // 4,
        "table_spread_min": table_spread,
        "table_spread_ideal": min(R, N // 4),
        "max_consecutive_wind": max_consec,
    }
    if has_teams:
        team_meet = Counter()
        for ds in by_round_table.values():
            teams = sorted({_team_of(d) for d in ds})
            for a, b in combinations(teams, 2):
                team_meet[(a, b)] += 1
        if team_meet:
            vals = list(team_meet.values())
            result["team_meet_min"] = min(vals)
            result["team_meet_max"] = max(vals)
    return result


def headline(m):
    """One-line human verdict from a :func:`measure` dict."""
    if not m["all_seated"]:
        return "Invalid chart: not every player is seated once per round."
    if m.get("no_teammates") is False:
        return f"Warning: teammates share a table {m['teammate_clashes']} time(s)."
    if m["max_meetings"] <= 1:
        return "Optimal: no rematches, winds balanced."
    return (f"Best achievable: {m['repeated_pairs']} pair(s) meet more than once "
            f"(rematch-free is impossible at this size and round count).")
