"""Pure scoring/stats helpers — no request/view dependencies.

Each public function takes (tenant, variables, ...) and returns the same data
shape as the corresponding view previously produced. Golden-file tests in
tests/test_scoring_golden.py lock the output shape.
"""
from collections import defaultdict
from functools import lru_cache
from itertools import groupby

import pycountry
from django.db.models import Q

from .models import Hand, Player, Position, PublishedRound, Schedule


WINDS = ('East', 'South', 'West', 'North')
COMPLETION_HAND_NB = 17  # a hand_nb=17 row with pts=0 marks a (round, table) as not yet final


# ---- public API -----------------------------------------------------------

def scores_per_table(tenant, variables):
    """Nested list [round][table][position] = {} or {'position': Position}."""
    nb_tables = Player.objects.filter(tenant=tenant).count() // 4
    grid = [
        [[{} for _ in range(4)] for _ in range(nb_tables)]
        for _ in range(variables.nb_rounds)
    ]
    for p in Position.objects.filter(tenant=tenant).select_related('player').order_by('id'):
        grid[p.round_nb - 1][p.table_nb - 1][p.position - 1] = {'position': p}
    return grid


def round_winners(tenant, variables, check_final=False, positions=None, hands=None):
    """Per-round top minipoints / single-hand score / same-seat-win streaks."""
    if check_final:
        round_max = _last_published_round(tenant)
        reveal = _last_round_reveal(tenant, variables.nb_rounds)
        # Last round published but podium reveal still in progress — hide everything.
        if round_max == variables.nb_rounds and (reveal is None or reveal <= 11):
            return []
    else:
        round_max = _last_complete_round(tenant, variables)

    if positions is None:
        positions = list(
            Position.objects.filter(tenant=tenant, round_nb__lte=round_max).select_related('player')
        )
    if hands is None:
        hands = list(Hand.objects.filter(tenant=tenant, round_nb__lte=round_max))

    pos_by_round = _group_by(
        (p for p in positions if p.round_nb <= round_max),
        key=lambda p: p.round_nb,
    )
    hand_by_round = _group_by(
        (h for h in hands if h.round_nb <= round_max),
        key=lambda h: h.round_nb,
    )

    return [
        _winners_for_round(pos_by_round[rn], hand_by_round[rn])
        for rn in range(1, round_max + 1)
    ]


def overall_winners(tenant, variables, positions=None, hands=None):
    """Aggregate round_winners across rounds: items from rounds tying for overall top."""
    rounds = round_winners(tenant, variables, positions=positions, hands=hands)
    return {
        'mp_max':        _roll_up(rounds, 'mp_max',        lambda p: p.minipoints),
        'sd_hand_max':   _roll_up(rounds, 'sd_hand_max',   lambda h: h['pts']),
        'ron_hand_max':  _roll_up(rounds, 'ron_hand_max',  lambda h: h['pts']),
        'sd_win_max':    _roll_up(rounds, 'sd_win_max',    lambda d: d['nb_win']),
        'ron_win_max':   _roll_up(rounds, 'ron_win_max',   lambda d: d['nb_win']),
        'total_win_max': _roll_up(rounds, 'total_win_max', lambda d: d['nb_win']),
    }


def player_rounds(tenant, player):
    """Per-round info for one player: own seat wind, opponents, schedule, completion flag."""
    schedule = [
        s for s in Schedule.objects.filter(tenant=tenant).order_by('id')
        if 'Round' in s.name or 'Session' in s.name
    ]
    my_positions = list(Position.objects.filter(tenant=tenant, player=player).order_by('round_nb'))
    if not my_positions:
        return []

    rounds_set = {p.round_nb for p in my_positions}
    positions_by_rt = _group_by(
        Position.objects.filter(tenant=tenant, round_nb__in=rounds_set)
                        .select_related('player').order_by('position'),
        key=lambda p: (p.round_nb, p.table_nb),
    )
    completed = {
        (h.round_nb, h.table_nb)
        for h in Hand.objects.filter(tenant=tenant, hand_nb=COMPLETION_HAND_NB, pts=1)
    }

    return [
        {
            'other_pos': positions_by_rt[(p.round_nb, p.table_nb)],
            'player_pos': WINDS[p.position - 1],
            'time': schedule[p.round_nb - 1].time,
            'day': schedule[p.round_nb - 1].day,
            'name': schedule[p.round_nb - 1].name,
            'detailed_hands': (p.round_nb, p.table_nb) in completed,
        }
        for p in my_positions
    ]


def player_standings(tenant, variables, check_final=True, force_all=False, positions=None):
    """Cumulative player totals with rank evolution across rounds."""
    players = list(Player.objects.filter(tenant=tenant).order_by('rand_id'))

    if positions is None:
        positions = list(Position.objects.filter(tenant=tenant).order_by('round_nb'))

    positions_by_player = defaultdict(list)
    round_max = variables.nb_rounds
    for pos in positions:
        positions_by_player[pos.player_id].append(pos)
        if pos.minipoints is None or pos.tablepoints is None:
            round_max = min(round_max, pos.round_nb - 1)

    # One query for both: highest published round and last-round reveal level.
    pub_rows = {
        r.round_nb: r.reveal_level
        for r in PublishedRound.objects.filter(tenant=tenant)
    }
    last_published = max(pub_rows) if pub_rows else 0
    reveal = pub_rows.get(variables.nb_rounds)  # None if last round not published

    # Public viewers only see rounds that have been explicitly published.
    if check_final and not force_all:
        round_max = min(round_max, last_published)

    # End-of-tournament suspense: last round published but podium reveal <= 11.
    # Public viewers see standings through round_max-1 only during the ceremony.
    end_of_tournament = (
        round_max == variables.nb_rounds and not force_all
        and reveal is not None and reveal <= 11
    )
    if end_of_tournament and check_final:
        round_max = max(0, round_max - 1)

    flags = {p.id: _country_flag(p.country) for p in players}
    history = {p.id: [1] for p in players}

    sort_key = (lambda s: (-s['total']['tp'], -s['total']['mp'])) if variables.rules == 'MCR' \
               else (lambda s: -s['total']['mp'])
    rank_key = lambda s: (s['total']['mp'], s['total']['tp'])

    ranked = []
    for current_round in range(round_max + 1):
        ranked = [
            _cumulative_row(p, positions_by_player[p.id], current_round, flags[p.id])
            for p in players
        ]
        ranked.sort(key=sort_key)
        _assign_ranks(ranked, rank_key, field='pos')
        _assign_ranks([r for r in ranked if r['country'].strip() == 'Sweden'],
                      rank_key, field='pos_se')
        for r in ranked:
            history[r['player_id']].append(r['pos'])

    for r in ranked:
        r['history_pos'] = history[r['player_id']]

    # Admin viewers (check_final=False) still get the full standings,
    # but rows outside the revealed podium window are marked not visible.
    if end_of_tournament and not check_final:
        reveal_lvl = reveal or 0
        for r in ranked:
            r['visible'] = 10 - (reveal_lvl - 1) < r['pos'] <= 10

    return ranked


def tournament_seating(tenant, variables, check_final=True, force_all=False, valid_pairs=None, positions=None):
    """seating grid + player→table lookup. Applies the same end-of-tournament
    masking as player_standings: when the last round is published but the
    podium reveal hasn't completed, check_final viewers see the final round's
    seats without MP/TP. Public viewers also see MP/TP masked for any
    unpublished round.
    """
    if positions is None:
        position_vals = list(
            Position.objects.filter(tenant=tenant).select_related('player').order_by('id')
        )
    else:
        position_vals = positions
    round_max = max((p.round_nb for p in position_vals), default=0)
    table_max = max((p.table_nb for p in position_vals), default=0)

    pub_rows = {r.round_nb: r.reveal_level for r in PublishedRound.objects.filter(tenant=tenant)}
    last_complete = _last_complete_round(tenant, variables)
    last_published = max(pub_rows) if pub_rows else 0
    reveal = pub_rows.get(variables.nb_rounds)
    end_of_tournament = (
        last_complete == variables.nb_rounds and not force_all
        and reveal is not None and reveal <= 11
    )
    hide_scores_round = last_complete if (end_of_tournament and check_final) else None

    player_table = {(p.player_id, p.round_nb): p.table_nb for p in position_vals}

    grid = [[[None] * 4 for _ in range(table_max)] for _ in range(round_max)]
    for p in position_vals:
        grid[p.round_nb - 1][p.table_nb - 1][p.position - 1] = p

    seating = []
    for r_idx, round_positions in enumerate(grid):
        round_nb = r_idx + 1
        hide_scores = hide_scores_round == round_nb
        # Public viewers see scores only from published rounds.
        if check_final and not force_all and round_nb > last_published:
            hide_scores = True
        tables = []
        for t_idx, table in enumerate(round_positions):
            if all(pos is None for pos in table):
                continue
            seats = []
            for i in range(4):
                pos = table[i]
                mp = None if hide_scores or pos is None else pos.minipoints
                tp = None if hide_scores or pos is None or pos.tablepoints is None \
                    else float(pos.tablepoints)
                seats.append({
                    'wind': WINDS[i],
                    'player': pos.player if pos else None,
                    'mp': mp,
                    'tp': tp,
                })
            table_nb = t_idx + 1
            has_scores = (not hide_scores) and (
                valid_pairs is not None and (round_nb, table_nb) in valid_pairs
            )
            tables.append({'table_nb': table_nb, 'seats': seats, 'has_scores': has_scores})
        seating.append({'round_nb': round_nb, 'tables': tables})

    return seating, player_table


def player_extra_stats(tenant, player, variables):
    """Placement rates and win/loss hand stats for one player."""
    positions = list(
        Position.objects.filter(tenant=tenant, player=player, tablepoints__isnull=False)
        .order_by('round_nb')
    )

    # Placement rates — MCR: 4TP=1st, 2TP=2nd, 1TP=3rd, 0TP=4th.
    # For non-MCR, rank by minipoints within the table.
    if variables.rules == 'MCR':
        tp_to_place = {4.0: 1, 2.0: 2, 1.0: 3, 0.0: 4}
        counts = {1: 0, 2: 0, 3: 0, 4: 0}
        for pos in positions:
            place = tp_to_place.get(float(pos.tablepoints))
            if place:
                counts[place] += 1
    else:
        counts = {1: 0, 2: 0, 3: 0, 4: 0}
        rts = {(pos.round_nb, pos.table_nb) for pos in positions}
        table_positions = _group_by(
            Position.objects.filter(
                tenant=tenant,
                round_nb__in={p.round_nb for p in positions},
            ).filter(minipoints__isnull=False),
            key=lambda p: (p.round_nb, p.table_nb),
        )
        for pos in positions:
            key = (pos.round_nb, pos.table_nb)
            if key not in rts or pos.minipoints is None:
                continue
            peers = sorted(table_positions[key], key=lambda p: -p.minipoints)
            place = next((i + 1 for i, p in enumerate(peers) if p.id == pos.id), None)
            if place:
                counts[place] += 1

    total_rounds = sum(counts.values())
    placement = [
        {
            'place': p,
            'label': ['1st', '2nd', '3rd', '4th'][p - 1],
            'count': counts[p],
            'rate_pct': counts[p] / total_rounds * 100 if total_rounds else 0,
        }
        for p in (1, 2, 3, 4)
    ]

    # Win / loss hand stats.
    round_table_seat = {pos.round_nb: (pos.table_nb, pos.position) for pos in positions}
    hands = Hand.objects.filter(
        tenant=tenant,
        round_nb__in=list(round_table_seat.keys()),
        hand_nb__lt=COMPLETION_HAND_NB,
    )

    sd_win = ron_win = deal_in = sd_lose = total_hands = 0
    for h in hands:
        info = round_table_seat.get(h.round_nb)
        if info is None or h.table_nb != info[0] or h.pts == 0:
            continue
        seat = info[1]
        total_hands += 1
        is_sd = h.win_from == 0 or h.win_from == h.win_by
        if h.win_by == seat:
            if is_sd:
                sd_win += 1
            else:
                ron_win += 1
        elif not is_sd and h.win_from == seat:
            deal_in += 1
        elif h.win_by != seat and is_sd:
            sd_lose += 1

    def _pct(n):
        return n / total_hands if total_hands else 0

    hand_stats = [
        {'label': 'Win by self-draw',  'count': sd_win,  'rate_pct': _pct(sd_win)  * 100},
        {'label': 'Win by discard',    'count': ron_win, 'rate_pct': _pct(ron_win) * 100},
        {'label': 'Deal in',           'count': deal_in, 'rate_pct': _pct(deal_in) * 100},
        {'label': 'Lose to self-draw', 'count': sd_lose, 'rate_pct': _pct(sd_lose) * 100},
    ]

    return {
        'placement': placement,
        'total_rounds': total_rounds,
        'hand_stats': hand_stats,
        'total_hands': total_hands,
    }


def team_extra_stats(tenant, team_name, variables):
    """Placement rates and win/loss stats aggregated over all players in a team."""
    players = list(Player.objects.filter(tenant=tenant, team=team_name))
    if not players:
        return None

    player_ids = [p.id for p in players]
    positions = list(
        Position.objects.filter(tenant=tenant, player_id__in=player_ids, tablepoints__isnull=False)
        .order_by('round_nb')
    )

    if variables.rules == 'MCR':
        tp_to_place = {4.0: 1, 2.0: 2, 1.0: 3, 0.0: 4}
        counts = {1: 0, 2: 0, 3: 0, 4: 0}
        for pos in positions:
            place = tp_to_place.get(float(pos.tablepoints))
            if place:
                counts[place] += 1
    else:
        counts = {1: 0, 2: 0, 3: 0, 4: 0}
        table_positions = _group_by(
            Position.objects.filter(
                tenant=tenant,
                round_nb__in={p.round_nb for p in positions},
            ).filter(minipoints__isnull=False),
            key=lambda p: (p.round_nb, p.table_nb),
        )
        rts = {(pos.round_nb, pos.table_nb) for pos in positions}
        for pos in positions:
            key = (pos.round_nb, pos.table_nb)
            if key not in rts or pos.minipoints is None:
                continue
            peers = sorted(table_positions[key], key=lambda p: -p.minipoints)
            place = next((i + 1 for i, p in enumerate(peers) if p.id == pos.id), None)
            if place:
                counts[place] += 1

    total_rounds = sum(counts.values())
    placement = [
        {
            'place': p,
            'label': ['1st', '2nd', '3rd', '4th'][p - 1],
            'count': counts[p],
            'rate_pct': counts[p] / total_rounds * 100 if total_rounds else 0,
        }
        for p in (1, 2, 3, 4)
    ]

    round_table_seat = {}
    for pos in positions:
        round_table_seat.setdefault(pos.player_id, {})[pos.round_nb] = (pos.table_nb, pos.position)

    hands = Hand.objects.filter(
        tenant=tenant,
        round_nb__in=list({p.round_nb for p in positions}),
        hand_nb__lt=COMPLETION_HAND_NB,
    )

    sd_win = ron_win = deal_in = sd_lose = total_hands = 0
    for h in hands:
        for pid, rts_map in round_table_seat.items():
            info = rts_map.get(h.round_nb)
            if info is None or h.table_nb != info[0] or h.pts == 0:
                continue
            seat = info[1]
            total_hands += 1
            is_sd = h.win_from == 0 or h.win_from == h.win_by
            if h.win_by == seat:
                if is_sd:
                    sd_win += 1
                else:
                    ron_win += 1
            elif not is_sd and h.win_from == seat:
                deal_in += 1
            elif h.win_by != seat and is_sd:
                sd_lose += 1

    def _pct(n):
        return n / total_hands if total_hands else 0

    hand_stats = [
        {'label': 'Win by self-draw',  'count': sd_win,  'rate_pct': _pct(sd_win)  * 100},
        {'label': 'Win by discard',    'count': ron_win, 'rate_pct': _pct(ron_win) * 100},
        {'label': 'Deal in',           'count': deal_in, 'rate_pct': _pct(deal_in) * 100},
        {'label': 'Lose to self-draw', 'count': sd_lose, 'rate_pct': _pct(sd_lose) * 100},
    ]

    return {
        'team': team_name,
        'players': [{'id': p.id, 'full_name': p.full_name} for p in players],
        'placement': placement,
        'total_rounds': total_rounds,
        'hand_stats': hand_stats,
        'total_hands': total_hands,
    }


# ---- helpers --------------------------------------------------------------

def _last_complete_round(tenant, variables):
    first_incomplete = (
        Position.objects.filter(tenant=tenant)
        .filter(Q(tablepoints=None) | Q(minipoints=None))
        .order_by('round_nb')
        .values('round_nb')
        .first()
    )
    return (first_incomplete['round_nb'] - 1) if first_incomplete else variables.nb_rounds


def _last_published_round(tenant):
    """Highest published round_nb for this tenant, or 0 if none published."""
    row = PublishedRound.objects.filter(tenant=tenant).order_by('-round_nb').first()
    return row.round_nb if row else 0


def _last_round_reveal(tenant, nb_rounds):
    """reveal_level of the last-round PublishedRound row, or None if not published.

    Replaces `variables.final`. Semantic mapping:
      None  → last round not published (today's final == 0: hide it from public)
      0..11 → progressive podium reveal (positions 10 → 1)
      >11   → fully revealed
    """
    row = PublishedRound.objects.filter(tenant=tenant, round_nb=nb_rounds).first()
    return row.reveal_level if row else None


def _group_by(iterable, key):
    """Return a defaultdict(list) keyed by key(item). Missing keys return []."""
    out = defaultdict(list)
    for item in iterable:
        out[key(item)].append(item)
    return out


@lru_cache(maxsize=256)
def _country_flag(country):
    if country == "Independent":
        return 'mi'
    try:
        name = country.replace('The ', '').strip()
        if not name:
            return ''  # search_fuzzy('') matches an arbitrary country (gb); short-circuit
        match = pycountry.countries.get(name=name)
        if match is None:
            results = pycountry.countries.search_fuzzy(name)
            match = results[0] if results else None
        return match.alpha_2.lower() if match else ''
    except Exception:
        return ''


def _assign_ranks(rows, key, field):
    """In-place 1-indexed ranks with tie-sharing (1, 2, 2, 4). Rows must be pre-sorted."""
    index = 0
    for _, group in groupby(rows, key=key):
        members = list(group)
        for r in members:
            r[field] = index + 1
        index += len(members)


def _is_self_draw(h):
    """Self-draw: winner drew their own tile (win_from == 0 or win_from == win_by)."""
    return h.win_from == 0 or h.win_from == h.win_by


def _winners_for_round(positions, hands):
    mp_max = _top_by(positions, key=lambda p: p.minipoints)
    round_complete = not any(h.hand_nb == COMPLETION_HAND_NB and h.pts == 0 for h in hands)
    empty = {'mp_max': mp_max, 'sd_hand_max': [], 'ron_hand_max': [],
             'sd_win_max': [], 'ron_win_max': [], 'total_win_max': []}
    if not round_complete:
        return empty
    # Pre-resolve player from positions to avoid N+1 in template via Hand.win_by_player().
    pos_lookup = {(p.round_nb, p.table_nb, p.position): p.player for p in positions}
    game_hands = [h for h in hands if h.hand_nb != COMPLETION_HAND_NB and h.pts > 0]
    sd_hands = [h for h in game_hands if _is_self_draw(h)]
    ron_hands = [h for h in game_hands if not _is_self_draw(h)]
    def _hand_item(h):
        return {
            'pts': h.pts,
            'round_nb': h.round_nb,
            'table_nb': h.table_nb,
            'player': pos_lookup.get((h.round_nb, h.table_nb, h.win_by)),
        }
    return {
        'mp_max':        mp_max,
        'sd_hand_max':   [_hand_item(h) for h in _top_by(sd_hands,  key=lambda h: h.pts, exclude_zero=True)],
        'ron_hand_max':  [_hand_item(h) for h in _top_by(ron_hands, key=lambda h: h.pts, exclude_zero=True)],
        'sd_win_max':    _top_win_streaks(sd_hands, pos_lookup),
        'ron_win_max':   _top_win_streaks(ron_hands, pos_lookup),
        'total_win_max': _top_win_streaks(game_hands, pos_lookup),
    }


def _top_by(items, key, exclude_zero=False):
    """Items tied for max of key. Returns [] if empty, or if exclude_zero and max is 0."""
    if not items:
        return []
    top = max(key(x) for x in items)
    if exclude_zero and top == 0:
        return []
    return [x for x in items if key(x) == top]


def _top_win_streaks(hands, pos_lookup=None):
    """For each (table, winning-seat) pair, count wins; keep groups tying for max.

    `pos_lookup` maps (round_nb, table_nb, position) -> Player; when provided it
    resolves the winning player without triggering Hand.win_by_player()'s N+1.
    """
    if not hands:
        return []
    by_seat = defaultdict(list)
    for h in hands:
        by_seat[(h.table_nb, h.win_by)].append(h)
    tables = sorted({h.table_nb for h in hands})
    ordered = [by_seat.get((t, seat), []) for t in tables for seat in (1, 2, 3, 4)]
    max_wins = max(len(g) for g in ordered)
    if max_wins == 0:
        return []
    def _player_of(h):
        if pos_lookup is None:
            return h.win_by_player
        return pos_lookup.get((h.round_nb, h.table_nb, h.win_by))
    return [
        {'nb_win': len(g), 'player': _player_of(g[0]), 'pos': g[0]}
        for g in ordered if len(g) == max_wins
    ]


def _roll_up(rounds, category, value_of):
    """Across rounds, collect all items from rounds tying for the overall top value."""
    best_value, best_items = 0, []
    for rs in rounds:
        items = rs.get(category) or []
        if not items:
            continue
        v = value_of(items[0])
        if v > best_value:
            best_value, best_items = v, list(items)
        elif v == best_value:
            best_items += items
    return best_items


def _cumulative_row(player, all_positions, up_to_round, flag):
    played = [p for p in all_positions if p.round_nb <= up_to_round]
    return {
        'history_pos': [1], 'visible': True, 'pos': 0, 'pos_se': '',
        'player_id': player.id, 'EMA_ID': player.EMA_ID,
        'first_name': player.first_name, 'last_name': player.last_name(),
        'name': player.full_name, 'country': player.country, 'flag': flag,
        'team': player.team,
        'scores': [{'mp': p.minipoints, 'tp': p.tablepoints} for p in played],
        'total': {
            'mp': sum(p.minipoints for p in played),
            'tp': sum(p.tablepoints for p in played),
        },
    }
