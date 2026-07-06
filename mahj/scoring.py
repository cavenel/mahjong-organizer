"""Pure scoring/stats helpers — no request/view dependencies.

Each public function takes (tenant, variables, ...) and returns the same data
shape as the corresponding view previously produced. Golden-file tests in
tests/test_scoring_golden.py lock the output shape.
"""
from collections import defaultdict
from functools import lru_cache
from itertools import groupby

import pycountry
from django.db.models import Q, Sum

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
        # Public viewers see the same rounds as the standings and the detail
        # modals: capped at the last published round, with the withheld final
        # round dropped during the ceremony-pending (reveal==0) window.
        round_max = public_round_max(tenant, variables, force_all=False)
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


def overall_winners(tenant, variables, check_final=False, positions=None, hands=None):
    """Aggregate round_winners across rounds: items from rounds tying for overall top.

    ``check_final`` is forwarded to ``round_winners`` so the overall roll-up honours
    the same end-of-tournament masking as the per-round stats: while the final round
    is prepared but not yet published, its hands/scores stay out of these cards.
    """
    rounds = round_winners(tenant, variables, check_final, positions=positions, hands=hands)
    return {
        'mp_max':        _roll_up(rounds, 'mp_max',        lambda p: p.minipoints),
        'hand_max':      _roll_up(rounds, 'hand_max',      lambda h: h['pts']),
        'sd_hand_max':   _roll_up(rounds, 'sd_hand_max',   lambda h: h['pts']),
        'ron_hand_max':  _roll_up(rounds, 'ron_hand_max',  lambda h: h['pts']),
        'sd_win_max':    _roll_up(rounds, 'sd_win_max',    lambda d: d['nb_win']),
        'ron_win_max':   _roll_up(rounds, 'ron_win_max',   lambda d: d['nb_win']),
        'total_win_max': _roll_up(rounds, 'total_win_max', lambda d: d['nb_win']),
    }


def table_stats(tenant, variables, check_final=False, positions=None, hands=None):
    """Validated-table completion + per-player deal-in ("From") ratios.

    Scope: only tables carrying a hand_nb=17, pts=1 validation marker, capped at
    the same published round as the other stats. Draws and unplayed
    slots are indistinguishable in the data (both pts=0), so hands-played is read
    as the index of the last hand with pts>0 — trailing zeros count as not played.
    """
    if check_final:
        round_max = public_round_max(tenant, variables, force_all=False)
    else:
        round_max = _last_complete_round(tenant, variables)

    if positions is None:
        positions = list(
            Position.objects.filter(tenant=tenant, round_nb__lte=round_max).select_related('player')
        )
    if hands is None:
        hands = list(Hand.objects.filter(tenant=tenant, round_nb__lte=round_max))

    valid = _validated_tables(hands, round_max)
    return _table_stats_for(positions, hands, valid)


def table_stats_rounds(tenant, variables, check_final=False, positions=None, hands=None):
    """Per-round version of table_stats: one dict per round, like round_winners."""
    if check_final:
        round_max = public_round_max(tenant, variables, force_all=False)
    else:
        round_max = _last_complete_round(tenant, variables)

    if positions is None:
        positions = list(
            Position.objects.filter(tenant=tenant, round_nb__lte=round_max).select_related('player')
        )
    if hands is None:
        hands = list(Hand.objects.filter(tenant=tenant, round_nb__lte=round_max))

    valid = _validated_tables(hands, round_max)
    pos_by_round = _group_by((p for p in positions if p.round_nb <= round_max), key=lambda p: p.round_nb)
    hand_by_round = _group_by((h for h in hands if h.round_nb <= round_max), key=lambda h: h.round_nb)
    return [
        _table_stats_for(
            pos_by_round[rn], hand_by_round[rn],
            {(r, t) for (r, t) in valid if r == rn},
        )
        for rn in range(1, round_max + 1)
    ]


def _validated_tables(hands, round_max):
    """(round, table) pairs with a hand_nb=17, pts=1 marker, capped at round_max.

    Derived from the in-memory hands (same rule as completed_tables / public.py's
    valid_pairs) so the cached-HTML path fires no extra query.
    """
    return {
        (h.round_nb, h.table_nb) for h in hands
        if h.hand_nb == COMPLETION_HAND_NB and h.pts == 1 and h.round_nb <= round_max
    }


def _table_stats_for(positions, hands, valid):
    """Table-completion + deal-in ("From") ratios over the given validated tables.

    Draws and unplayed slots are indistinguishable (both pts=0), so hands-played is
    read as the index of the last hand with pts>0 — trailing zeros count as unplayed.
    """
    hand_by_table = _group_by(
        (h for h in hands if (h.round_nb, h.table_nb) in valid),
        key=lambda h: (h.round_nb, h.table_nb),
    )
    hands_played = {
        rt: max(
            (h.hand_nb for h in hand_by_table[rt]
             if h.hand_nb < COMPLETION_HAND_NB and h.pts > 0),
            default=0,
        )
        for rt in valid
    }

    tables_total = len(valid)
    tables_finished = sum(1 for n in hands_played.values() if n == 16)
    avg_hands = round(sum(hands_played.values()) / tables_total, 1) if tables_total else 0

    # Per-player win/luck tallies from every game hand on a validated table. A seat
    # (win_by / win_from) is resolved to a player via the position lookup, the same
    # N+1-avoidance as _hand_item.
    #   deal_ins    — gave the winning tile (a discard win from another seat)
    #   self_draws  — won by self-draw (the "luckiest": no one had to feed them)
    #   sd_victims  — sat through someone else's self-draw (the "unluckiest": paid
    #                 out without dealing in; all three non-winners are victims)
    # Alongside them, the tournament-wide average value of a won hand.
    pos_lookup = {(p.round_nb, p.table_nb, p.position): p.player for p in positions}
    deal_ins = defaultdict(int)
    self_draws = defaultdict(int)
    sd_victims = defaultdict(int)
    won_pts = won_count = 0
    for rt, table_hands in hand_by_table.items():
        for h in table_hands:
            if h.hand_nb >= COMPLETION_HAND_NB or h.pts <= 0:
                continue
            won_pts += h.pts
            won_count += 1
            if _is_self_draw(h):
                winner = pos_lookup.get((h.round_nb, h.table_nb, h.win_by))
                if winner is not None:
                    self_draws[winner] += 1
                for seat in (1, 2, 3, 4):
                    if seat == h.win_by:
                        continue
                    victim = pos_lookup.get((h.round_nb, h.table_nb, seat))
                    if victim is not None:
                        sd_victims[victim] += 1
            else:
                giver = pos_lookup.get((h.round_nb, h.table_nb, h.win_from))
                if giver is not None:
                    deal_ins[giver] += 1

    avg_hand_value = round(won_pts / won_count, 1) if won_count else 0

    # Hands played by a player = the played-count of every validated table they sat at.
    played = defaultdict(int)
    for p in positions:
        rt = (p.round_nb, p.table_nb)
        if rt in valid:
            played[p.player] += hands_played[rt]

    def _ratio_items(tally):
        return [
            {'player': player, 'count': tally.get(player, 0), 'nb_hands': n,
             'pct': round(100 * tally.get(player, 0) / n, 1)}
            for player, n in played.items() if n > 0
        ]

    deal_in_items = _ratio_items(deal_ins)
    gave_most = sorted(deal_in_items, key=lambda d: d['pct'], reverse=True)[:5]
    gave_least = sorted(deal_in_items, key=lambda d: d['pct'])[:5]
    luckiest = sorted(_ratio_items(self_draws), key=lambda d: d['pct'], reverse=True)[:5]
    unluckiest = sorted(_ratio_items(sd_victims), key=lambda d: d['pct'], reverse=True)[:5]

    return {
        'tables_finished': tables_finished,
        'tables_total': tables_total,
        'avg_hands': avg_hands,
        'avg_hand_value': avg_hand_value,
        'nb_won': won_count,
        'gave_most': gave_most,
        'gave_least': gave_least,
        'luckiest': luckiest,
        'unluckiest': unluckiest,
    }


def player_schedule(tenant):
    """The round/session rows used by player_rounds, fetched once."""
    return [
        s for s in Schedule.objects.filter(tenant=tenant).order_by('id')
        if 'Round' in s.name or 'Session' in s.name
    ]


def completed_tables(tenant):
    """Set of (round_nb, table_nb) that have a completion hand recorded."""
    return {
        (h.round_nb, h.table_nb)
        for h in Hand.objects.filter(tenant=tenant, hand_nb=COMPLETION_HAND_NB, pts=1)
    }


def player_rounds(tenant, player, schedule=None, completed=None):
    """Per-round info for one player: own seat wind, opponents, schedule, completion flag.

    ``schedule`` and ``completed`` are tenant-wide and identical for every player;
    pass them in when looping over many players to avoid re-querying per player.
    """
    if schedule is None:
        schedule = player_schedule(tenant)
    if completed is None:
        completed = completed_tables(tenant)

    my_positions = list(Position.objects.filter(tenant=tenant, player=player).order_by('round_nb'))
    if not my_positions:
        return []

    rounds_set = {p.round_nb for p in my_positions}
    positions_by_rt = _group_by(
        Position.objects.filter(tenant=tenant, round_nb__in=rounds_set)
                        .select_related('player').order_by('position'),
        key=lambda p: (p.round_nb, p.table_nb),
    )

    return _rounds_for(my_positions, positions_by_rt, schedule, completed)


def _rounds_for(my_positions, positions_by_rt, schedule, completed):
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


def all_player_rounds(tenant, players):
    """player_rounds for many players in a constant number of queries.

    player_rounds re-queries a large slice of the Position table for every
    player; over ~168 players that materializes hundreds of thousands of rows.
    Here every player's positions come from one query, grouped once and sliced
    per player. Returns {player_id: rounds_list}.
    """
    schedule = player_schedule(tenant)
    completed = completed_tables(tenant)

    all_positions = list(
        Position.objects.filter(tenant=tenant)
                        .select_related('player').order_by('round_nb', 'position')
    )
    positions_by_rt = _group_by(all_positions, key=lambda p: (p.round_nb, p.table_nb))
    positions_by_player = _group_by(all_positions, key=lambda p: p.player_id)

    return {
        player.id: _rounds_for(
            positions_by_player.get(player.id, []), positions_by_rt, schedule, completed,
        )
        for player in players
    }


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

    # End-of-tournament suspense: the last round is in the pre-publish state
    # (reveal == 0) — prepared for the ceremony but withheld from the public.
    # Public viewers see standings through round_max-1 until it's published.
    end_of_tournament = (
        round_max == variables.nb_rounds and not force_all
        and reveal == 0
    )
    if end_of_tournament and check_final:
        round_max = max(0, round_max - 1)

    flags = {p.id: _country_flag(p.country) for p in players}
    history = {p.id: [1] for p in players}

    sort_key = _standings_sort_key(variables)
    rank_key = _standings_rank_key(variables)

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

    # Admin/display viewers (check_final=False) get the full standings, but
    # every row is masked while the last round is unpublished. The reveal
    # animation is the ceremony page's job, so these rows stay hidden until
    # the results are published.
    if end_of_tournament and not check_final:
        for r in ranked:
            r['visible'] = False

    return ranked


def team_standings(rows, variables, nb_rounds):
    """Aggregate per-player standing rows into ranked team rows.

    `rows` are player rows as produced by `player_standings` / desktop, each
    carrying 'team', 'flag', 'player_id', 'total' {tp, mp} and per-round
    'scores' [{tp, mp}]. Returns team rows sorted by the active rules, each with
    'team', 'flag', 'player_ids', 'total', per-round 'scores' and a 1-based 'pos'
    that ties share — teams level on both TP and MP get the same position, just
    like players.
    """
    by_team = {}
    for s in rows:
        t = s.get('team') or ''
        if not t:
            continue
        slot = by_team.setdefault(t, {
            'team': t,
            'player_ids': [],
            '_flags': set(),
            'flag': '',
            'total': {'tp': 0.0, 'mp': 0},
            'scores': [{'tp': None, 'mp': None, 'round_nb': r} for r in range(1, nb_rounds + 1)],
        })
        slot['player_ids'].append(s['player_id'])
        slot['_flags'].add(s.get('flag') or '')
        slot['total']['tp'] += s['total'].get('tp') or 0
        slot['total']['mp'] += s['total'].get('mp') or 0
        for r_idx, sc in enumerate(s['scores']):
            if r_idx < len(slot['scores']) and sc.get('tp') is not None:
                rslot = slot['scores'][r_idx]
                rslot['tp'] = (rslot['tp'] or 0) + sc['tp']
                rslot['mp'] = (rslot['mp'] or 0) + (sc.get('mp') or 0)
    team_rows = sorted(by_team.values(), key=_standings_sort_key(variables))
    _assign_ranks(team_rows, _standings_rank_key(variables), field='pos')
    for tr in team_rows:
        flags = tr.pop('_flags')
        tr['flag'] = next(iter(flags)) if len(flags) == 1 else ''
    return team_rows


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
        and reveal == 0
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


def player_extra_stats(tenant, player, variables, max_round=None):
    """Placement rates and win/loss hand stats for one player.

    `max_round` caps the rounds folded in: a public viewer must not see a
    withheld final round leak into these cards (the per-round score grid in the
    same modal already hides it). None = no cap, for admin/ceremony callers.
    """
    qs = Position.objects.filter(tenant=tenant, player=player, tablepoints__isnull=False)
    if max_round is not None:
        qs = qs.filter(round_nb__lte=max_round)
    positions = list(qs.order_by('round_nb'))

    counts = _placement_counts(tenant, positions, variables)
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

    # Win / loss hand stats. Only count hands from validated score sheets — an
    # un-validated table's hand detail (e.g. freshly scanned, not yet human-checked)
    # must not feed these rates, exactly like the per-table detailed-hands modal.
    completed = completed_tables(tenant)
    round_table_seat = {pos.round_nb: (pos.table_nb, pos.position) for pos in positions}
    hands = list(Hand.objects.filter(
        tenant=tenant,
        round_nb__in=list(round_table_seat.keys()),
        hand_nb__lt=COMPLETION_HAND_NB,
    ))

    # A genuine draw and an unplayed trailing slot are both pts=0. Only hands up to
    # the last decided hand of the table were actually played; hands_played[round]
    # is that last hand_nb, so a pts=0 hand after it is an unplayed slot, not a draw.
    hands_played = defaultdict(int)
    for h in hands:
        info = round_table_seat.get(h.round_nb)
        if info is None or h.table_nb != info[0] or (h.round_nb, h.table_nb) not in completed:
            continue
        if h.pts > 0 and h.hand_nb > hands_played[h.round_nb]:
            hands_played[h.round_nb] = h.hand_nb

    sd_win = ron_win = deal_in = sd_lose = draw = total_hands = 0
    # Value (pts) of the hands this player won, kept per round so the modal can show
    # a per-round average alongside the tournament average.
    won_pts = defaultdict(int)
    won_count = defaultdict(int)
    for h in hands:
        info = round_table_seat.get(h.round_nb)
        if info is None or h.table_nb != info[0]:
            continue
        if (h.round_nb, h.table_nb) not in completed:
            continue
        seat = info[1]
        if h.pts == 0:
            # Mid-table draw counts as a played hand; a trailing unplayed slot doesn't.
            if h.hand_nb <= hands_played[h.round_nb]:
                total_hands += 1
                draw += 1
            continue
        total_hands += 1
        is_sd = h.win_from == 0 or h.win_from == h.win_by
        if h.win_by == seat:
            won_pts[h.round_nb] += h.pts
            won_count[h.round_nb] += 1
            if is_sd:
                sd_win += 1
            else:
                ron_win += 1
        elif not is_sd and h.win_from == seat:
            deal_in += 1
        elif h.win_by != seat and is_sd:
            sd_lose += 1

    hand_value = [
        {
            'round_nb': rn,
            'count': won_count[rn],
            'avg': won_pts[rn] / won_count[rn] if won_count[rn] else None,
        }
        for rn in sorted(round_table_seat)
    ]
    total_won = sum(won_count.values())
    avg_hand_value = sum(won_pts.values()) / total_won if total_won else None

    def _pct(n):
        return n / total_hands if total_hands else 0

    hand_stats = [
        {'label': 'Win by self-draw',  'count': sd_win,  'rate_pct': _pct(sd_win)  * 100},
        {'label': 'Win by discard',    'count': ron_win, 'rate_pct': _pct(ron_win) * 100},
        {'label': 'Deal in',           'count': deal_in, 'rate_pct': _pct(deal_in) * 100},
        {'label': 'Lose to self-draw', 'count': sd_lose, 'rate_pct': _pct(sd_lose) * 100},
        {'label': 'Draw',              'count': draw,    'rate_pct': _pct(draw)    * 100},
    ]

    return {
        'placement': placement,
        'total_rounds': total_rounds,
        'hand_stats': hand_stats,
        'total_hands': total_hands,
        'hand_value': hand_value,
        'avg_hand_value': avg_hand_value,
        'total_won': total_won,
        'opp_strength': _opponent_strength(tenant, positions, variables, max_round),
    }


def team_extra_stats(tenant, team_name, variables, max_round=None):
    """Placement rates and win/loss stats aggregated over all players in a team.

    `max_round` caps the rounds folded in, exactly like `player_extra_stats` —
    public team modals must not leak a withheld final round. None = no cap.
    """
    players = list(Player.objects.filter(tenant=tenant, team=team_name))
    if not players:
        return None

    player_ids = [p.id for p in players]
    qs = Position.objects.filter(tenant=tenant, player_id__in=player_ids, tablepoints__isnull=False)
    if max_round is not None:
        qs = qs.filter(round_nb__lte=max_round)
    positions = list(qs.order_by('round_nb'))

    counts = _placement_counts(tenant, positions, variables)
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

    # Only validated score sheets feed the win/loss rates (see player_extra_stats).
    completed = completed_tables(tenant)
    round_table_seat = {}
    for pos in positions:
        round_table_seat.setdefault(pos.player_id, {})[pos.round_nb] = (pos.table_nb, pos.position)

    hands = list(Hand.objects.filter(
        tenant=tenant,
        round_nb__in=list({p.round_nb for p in positions}),
        hand_nb__lt=COMPLETION_HAND_NB,
    ))

    # Mid-table draws are played hands; trailing pts=0 slots are unplayed. Per table,
    # hands_played is the last decided hand_nb (see player_extra_stats for the why).
    hands_played = defaultdict(int)
    for h in hands:
        if (h.round_nb, h.table_nb) in completed and h.pts > 0 \
                and h.hand_nb > hands_played[(h.round_nb, h.table_nb)]:
            hands_played[(h.round_nb, h.table_nb)] = h.hand_nb

    sd_win = ron_win = deal_in = sd_lose = draw = total_hands = 0
    for h in hands:
        if (h.round_nb, h.table_nb) not in completed:
            continue
        is_draw = h.pts == 0
        if is_draw and h.hand_nb > hands_played[(h.round_nb, h.table_nb)]:
            continue  # unplayed trailing slot, not a real draw
        for pid, rts_map in round_table_seat.items():
            info = rts_map.get(h.round_nb)
            if info is None or h.table_nb != info[0]:
                continue
            seat = info[1]
            total_hands += 1
            if is_draw:
                draw += 1
                continue
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
        {'label': 'Draw',              'count': draw,    'rate_pct': _pct(draw)    * 100},
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
    """Publish state of the last round, read from its PublishedRound row.

    One of three values:
      None → last round not published at all
      0    → pre-publish: results prepared for the ceremony but withheld from
             the public (standings stay masked)
      100  → published to everyone (results public)

    The live podium animation is driven by the ceremony page; this is only the
    publish flag, so 0 and 100 are the only non-None values that occur.
    """
    row = PublishedRound.objects.filter(tenant=tenant, round_nb=nb_rounds).first()
    return row.reveal_level if row else None


def public_round_max(tenant, variables, force_all=False):
    """Highest round number a public viewer may see, mirroring `player_standings`.

    Public viewers (`force_all=False`) are clamped to the last published round,
    and during the end-of-tournament suspense window (final round prepared but
    held back, reveal==0) one further round is dropped. `force_all=True` (admin
    /ceremony) sees every scored round. Use this to cap auxiliary surfaces — e.g.
    the modal's placement/hand cards — to the same rounds the standings expose.
    """
    if force_all:
        return variables.nb_rounds
    round_max = min(_last_complete_round(tenant, variables), _last_published_round(tenant))
    if round_max == variables.nb_rounds and _last_round_reveal(tenant, variables.nb_rounds) == 0:
        round_max = max(0, round_max - 1)
    return round_max


def _placement_counts(tenant, positions, variables):
    """How often these positions placed 1st/2nd/3rd/4th at their own table.

    A seat's place is its rank within its (round, table): by table points for MCR,
    by minipoints otherwise. Tied seats share a place (1, 1, 3, 4 — standard
    competition ranking), so a tie for 1st counts both as 1st and no round is ever
    dropped from the stats.

    `positions` are this player's/team's seats (already filtered to scored rounds);
    the table peers are fetched per round so every seat can be ranked against its
    own table.
    """
    rank_field = 'tablepoints' if variables.rules == 'MCR' else 'minipoints'
    table_positions = _group_by(
        Position.objects.filter(
            tenant=tenant,
            round_nb__in={p.round_nb for p in positions},
            **{f'{rank_field}__isnull': False},
        ),
        key=lambda p: (p.round_nb, p.table_nb),
    )
    counts = {1: 0, 2: 0, 3: 0, 4: 0}
    for pos in positions:
        my_val = getattr(pos, rank_field)
        if my_val is None:
            continue
        peers = table_positions[(pos.round_nb, pos.table_nb)]
        place = 1 + sum(1 for p in peers if getattr(p, rank_field) > my_val)
        if place <= 4:
            counts[place] += 1
    return counts


def _opponent_strength(tenant, positions, variables, max_round=None):
    """Strength of schedule: total & average full-tournament table points of
    every opponent this player faced (an opponent faced twice counts twice).

    Mirrors _placement_counts: ranks on tablepoints for MCR, minipoints
    otherwise. `max_round` caps the opponents' totals to the rounds this modal
    is allowed to show, so a withheld final round can't leak in.
    """
    if not positions:
        return {'total': 0, 'avg': 0, 'count': 0}
    field = 'tablepoints' if variables.rules == 'MCR' else 'minipoints'
    totals_qs = Position.objects.filter(tenant=tenant, **{f'{field}__isnull': False})
    if max_round is not None:
        totals_qs = totals_qs.filter(round_nb__lte=max_round)
    totals = dict(totals_qs.values_list('player_id').annotate(Sum(field)))

    table_positions = _group_by(
        Position.objects.filter(
            tenant=tenant, round_nb__in={p.round_nb for p in positions},
        ),
        key=lambda p: (p.round_nb, p.table_nb),
    )
    total = 0.0
    count = 0
    for pos in positions:
        for peer in table_positions[(pos.round_nb, pos.table_nb)]:
            if peer.player_id == pos.player_id:
                continue
            total += totals.get(peer.player_id, 0) or 0
            count += 1
    return {'total': total, 'avg': total / count if count else 0, 'count': count}


def _group_by(iterable, key):
    """Return a defaultdict(list) keyed by key(item). Missing keys return []."""
    out = defaultdict(list)
    for item in iterable:
        out[key(item)].append(item)
    return out


# Country names pycountry's exact + fuzzy lookup gets wrong or misses, mapped to
# their ISO alpha-2 flag code. "Turkey" misses entirely (pycountry renamed it
# "Türkiye"), so a Turkish player would get no flag and be excluded from "Best
# European". Bare "Korea" fuzzy-matches "Korea, Democratic People's Republic of"
# (kp, North Korea) instead of South Korea (kr). Keys are lower-cased and matched
# after stripping a leading "The ".
_FLAG_ALIASES = {
    'turkey': 'tr',
    'korea': 'kr',
    'south korea': 'kr',
    'chinese taipei': 'tw',
}


@lru_cache(maxsize=256)
def _country_flag(country):
    if country == "Independent":
        return 'mi'
    try:
        name = country.replace('The ', '').strip()
        if not name:
            return ''  # search_fuzzy('') matches an arbitrary country (gb); short-circuit
        alias = _FLAG_ALIASES.get(name.lower())
        if alias:
            return alias
        match = pycountry.countries.get(name=name)
        if match is None:
            results = pycountry.countries.search_fuzzy(name)
            match = results[0] if results else None
        return match.alpha_2.lower() if match else ''
    except Exception:
        return ''


def _standings_sort_key(variables):
    """Order standing rows best-first by the active rules. MCR ranks on TP (MP
    breaks ties); other rules rank on MP. Used for both players and teams so a
    team's row is ordered exactly like a player's."""
    if variables.rules == 'MCR':
        return lambda s: (-s['total']['tp'], -s['total']['mp'])
    return lambda s: -s['total']['mp']


def _standings_rank_key(variables):
    """Tie key for `_assign_ranks`, mirroring `_standings_sort_key` so rows tie
    (share a position) exactly when they're level on every value the active rules
    order by. MCR ranks on TP with MP as the tie-breaker, so a shared position
    needs both equal. Other rules rank on MP alone — equal MP alone ties, and TP
    must not split them, since the sort doesn't order by TP at all (rows level on
    MP keep their input order, so a (MP, TP) key would also assign positions
    non-deterministically)."""
    if variables.rules == 'MCR':
        return lambda s: (s['total']['tp'], s['total']['mp'])
    return lambda s: s['total']['mp']


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
    empty = {'mp_max': mp_max, 'hand_max': [], 'sd_hand_max': [], 'ron_hand_max': [],
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
        'hand_max':      [_hand_item(h) for h in _top_by(game_hands, key=lambda h: h.pts, exclude_zero=True)],
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
        {'nb_win': len(g), 'player': _player_of(g[0]), 'pos': g[0],
         'round_nb': g[0].round_nb, 'table_nb': g[0].table_nb}
        for g in ordered if len(g) == max_wins
    ]


def _roll_up(rounds, category, value_of):
    """Across rounds, collect all items from rounds tying for the overall top value.

    `best_value` starts as None (not 0) so the first non-empty round sets the bar:
    minipoints are zero-sum and routinely negative, and a 0 seed would either drop
    an all-negative field entirely or merge a spurious 0-valued round into it.
    """
    best_value, best_items = None, []
    for rs in rounds:
        items = rs.get(category) or []
        if not items:
            continue
        v = value_of(items[0])
        if best_value is None or v > best_value:
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
