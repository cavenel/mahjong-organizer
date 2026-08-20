"""Per-round and per-player/team statistics, the seating-grid table, and the
per-player rounds/opponents used by the modals and the projector cards.

Hand encoding (see docs/dev/data-model.md): ``win_by`` is a wind (1-4) for a win, 0
for a draw, and NULL for an unplayed placeholder; a win with ``win_from is None``
is a self-draw; ``points`` is its value. A validated score sheet stores exactly
the hands played (draws included, unplayed pruned), so "hands played at a table"
is simply its Hand row count — there is no unplayed-slot heuristic.
"""
from collections import defaultdict

from django.db.models import Sum

from ..models import Hand, Player, ScoreSheet, Seat
from ._common import (
    WINDS, _attach_players, _group_by, completed_tables, player_schedule,
    unscored_seats_q,
)
from .visibility import public_round_max, _last_complete_round
from .standings import player_standings


def scores_per_table(tenant, tournament):
    """Nested list [round][table][seat] = {} or {'seat': Seat}.

    Sized to hold every seat in the chart, not to the player count: a chart can
    legitimately run wider or longer than `players // 4` rounds of `nb_rounds` — a
    field that isn't a multiple of four, a partly-imported setup, a chart imported
    for more rounds than the settings name — and writing a seat outside a grid built
    to the smaller size raised IndexError, a 500 on a public page. The player-count
    and nb_rounds floors are kept so an empty chart still renders its blank tables.
    """
    seats = _attach_players(tenant, list(
        Seat.objects.filter(tenant=tenant).order_by('id')))
    nb_rounds = max([tournament.nb_rounds] + [p.round_nb for p in seats])
    nb_tables = max([Player.objects.filter(tenant=tenant).count() // 4]
                    + [p.table_nb for p in seats])
    grid = [
        [[{} for _ in range(4)] for _ in range(nb_tables)]
        for _ in range(nb_rounds)
    ]
    for p in seats:
        grid[p.round_nb - 1][p.table_nb - 1][p.wind - 1] = {'seat': p}
    return grid


# What one hand meant for the player sitting in one wind. Every hand/seat pair falls
# into exactly one of these, or None when the seat wasn't involved (someone else's
# discard win). Named once because four tallies ask the same question, and the rules
# are easy to restate subtly differently — a lost `h.is_self_draw` turns a self-draw
# into a deal-in for three seats at once.
HAND_DRAW = 'draw'          # nobody won
HAND_SELF_DRAW_WIN = 'sd_win'    # this seat won, off their own tile
HAND_DISCARD_WIN = 'ron_win'     # this seat won, off someone's discard
HAND_DEAL_IN = 'deal_in'         # this seat fed the winning tile
HAND_SELF_DRAW_LOSS = 'sd_lose'  # this seat paid out someone else's self-draw


def _place_within_table(value, peer_values):
    """A seat's finishing place at its own table, on whatever field the rules rank on.

    Standard competition ranking: count the peers who beat you and add one, so tied
    seats share a place (1, 1, 3, 4) and a tie for first is never dropped from the
    stats. Pass only scored peers — an unscored seat has no place at all.
    """
    return 1 + sum(1 for peer in peer_values if peer > value)


def classify_hand(hand, wind):
    """How ``hand`` counts for the player seated in ``wind`` (1-4).

    Returns one of the HAND_* constants, or None when this seat took no part —
    a discard win between two of the other three.
    """
    if hand.is_draw:
        return HAND_DRAW
    if hand.win_by == wind:
        return HAND_SELF_DRAW_WIN if hand.is_self_draw else HAND_DISCARD_WIN
    if hand.is_self_draw:
        return HAND_SELF_DRAW_LOSS
    if hand.win_from == wind:
        return HAND_DEAL_IN
    return None


def round_winners(tenant, tournament, full_view=False, seats=None, hands=None):
    """Per-round top minipoints / single-hand score / same-seat-win streaks."""
    if not full_view:
        # Public viewers see the same rounds as the standings and the detail
        # modals: capped at the last published round, with the withheld final
        # round dropped during the ceremony-pending window.
        round_max = public_round_max(tenant, tournament)
    else:
        round_max = _last_complete_round(tenant, tournament)

    if seats is None:
        seats = _attach_players(tenant, list(
            Seat.objects.filter(tenant=tenant, round_nb__lte=round_max)
        ))
    if hands is None:
        hands = list(Hand.objects.filter(tenant=tenant, round_nb__lte=round_max))

    # A round is "complete" (stats shown) once no table in it has an open,
    # not-yet-validated score sheet. Tables never opened don't block it.
    open_rounds = {
        s.round_nb for s in ScoreSheet.objects.filter(
            tenant=tenant, validated=False, round_nb__lte=round_max)
    }

    seats_by_round = _group_by(
        (p for p in seats if p.round_nb <= round_max),
        key=lambda p: p.round_nb,
    )
    hand_by_round = _group_by(
        (h for h in hands if h.round_nb <= round_max),
        key=lambda h: h.round_nb,
    )

    return [
        _winners_for_round(seats_by_round[rn], hand_by_round[rn], rn not in open_rounds)
        for rn in range(1, round_max + 1)
    ]


def overall_winners(tenant, tournament, full_view=False, seats=None, hands=None):
    """Aggregate round_winners across rounds: items from rounds tying for overall top.

    ``full_view`` is forwarded to ``round_winners`` so the overall roll-up honours
    the same end-of-tournament masking as the per-round stats: while the final round
    is prepared but not yet published, its hands/scores stay out of these cards.
    """
    rounds = round_winners(tenant, tournament, full_view, seats=seats, hands=hands)
    return {
        'mp_max':        _roll_up(rounds, 'mp_max',        lambda p: p.minipoints),
        'hand_max':      _roll_up(rounds, 'hand_max',      lambda h: h['points']),
        'sd_hand_max':   _roll_up(rounds, 'sd_hand_max',   lambda h: h['points']),
        'ron_hand_max':  _roll_up(rounds, 'ron_hand_max',  lambda h: h['points']),
        'sd_win_max':    _roll_up(rounds, 'sd_win_max',    lambda d: d['nb_win']),
        'ron_win_max':   _roll_up(rounds, 'ron_win_max',   lambda d: d['nb_win']),
        'total_win_max': _roll_up(rounds, 'total_win_max', lambda d: d['nb_win']),
    }


def table_stats(tenant, tournament, full_view=False, seats=None, hands=None):
    """Validated-table completion + per-player deal-in ("From") ratios.

    Scope: only validated score sheets, capped at the same published round as the
    other stats. A validated sheet stores exactly the hands played, so hands-played
    is just its Hand row count.
    """
    if not full_view:
        round_max = public_round_max(tenant, tournament)
    else:
        round_max = _last_complete_round(tenant, tournament)

    if seats is None:
        seats = _attach_players(tenant, list(
            Seat.objects.filter(tenant=tenant, round_nb__lte=round_max)
        ))
    if hands is None:
        hands = list(Hand.objects.filter(tenant=tenant, round_nb__lte=round_max))

    valid = _validated_tables(tenant, round_max)
    return _table_stats_for(seats, hands, valid)


def table_stats_rounds(tenant, tournament, full_view=False, seats=None, hands=None):
    """Per-round version of table_stats: one dict per round, like round_winners."""
    if not full_view:
        round_max = public_round_max(tenant, tournament)
    else:
        round_max = _last_complete_round(tenant, tournament)

    if seats is None:
        seats = _attach_players(tenant, list(
            Seat.objects.filter(tenant=tenant, round_nb__lte=round_max)
        ))
    if hands is None:
        hands = list(Hand.objects.filter(tenant=tenant, round_nb__lte=round_max))

    valid = _validated_tables(tenant, round_max)
    seats_by_round = _group_by((p for p in seats if p.round_nb <= round_max), key=lambda p: p.round_nb)
    hand_by_round = _group_by((h for h in hands if h.round_nb <= round_max), key=lambda h: h.round_nb)
    return [
        _table_stats_for(
            seats_by_round[rn], hand_by_round[rn],
            {(r, t) for (r, t) in valid if r == rn},
        )
        for rn in range(1, round_max + 1)
    ]


def stats_export(tenant, tournament, full_view=False, seats=None, hands=None):
    """One comprehensive per-player stats row for the 'Download stats' export.

    Folds together everything the player-detail modal and the tournament stats tab
    show — standings, per-round scores, placement, win/loss breakdown, self-draw
    luck, average/biggest hand, strength of schedule — computed in a single pass
    over the prefetched seats/hands (no per-player queries). Rounds are capped
    exactly like the on-screen stats: public viewers see published rounds, admins
    see every scored round.
    """
    if not full_view:
        round_max = public_round_max(tenant, tournament)
    else:
        round_max = _last_complete_round(tenant, tournament)

    if seats is None:
        seats = _attach_players(tenant, list(
            Seat.objects.filter(tenant=tenant).order_by('round_nb')
        ))
    if hands is None:
        hands = list(Hand.objects.filter(tenant=tenant))

    scored = [p for p in seats if p.round_nb <= round_max]
    valid = _validated_tables(tenant, round_max)
    rank_field = 'tablepoints' if tournament.rules == 'MCR' else 'minipoints'

    seat_lookup = {(p.round_nb, p.table_nb, p.wind): p.player for p in scored}
    seats_by_table = _group_by(scored, key=lambda p: (p.round_nb, p.table_nb))

    # Each player's full-tournament total of the ranking field (for strength of schedule).
    field_total = defaultdict(float)
    for p in scored:
        v = getattr(p, rank_field)
        if v is not None:
            field_total[p.player_id] += float(v)

    # Validated sheets store exactly the hands played, so a table's hands played is
    # its Hand row count and its draws are the hands with no winner.
    hand_by_table = _group_by(
        (h for h in hands if (h.round_nb, h.table_nb) in valid),
        key=lambda h: (h.round_nb, h.table_nb),
    )
    hands_played = {rt: len(hand_by_table[rt]) for rt in valid}

    total_hands = defaultdict(int)  # every played hand at a validated table they sat at
    draws = defaultdict(int)
    sd_win = defaultdict(int)
    ron_win = defaultdict(int)
    deal_in = defaultdict(int)
    sd_lose = defaultdict(int)      # sat through someone else's self-draw
    won_pts = defaultdict(int)
    biggest = defaultdict(int)

    for (r, t), table_hands in hand_by_table.items():
        hp = hands_played[(r, t)]
        decided = 0
        for h in table_hands:
            if h.is_draw:
                continue  # draw — credited to every seat via draw_table below
            decided += 1
            # Ask classify_hand once per seat rather than picking out the winner,
            # the giver and the victims by hand: same attribution, one definition
            # of the rules, shared with the player and team hand-stat panels.
            for wind in (1, 2, 3, 4):
                seated = seat_lookup.get((r, t, wind))
                if seated is None:
                    continue
                kind = classify_hand(h, wind)
                if kind in (HAND_SELF_DRAW_WIN, HAND_DISCARD_WIN):
                    won_pts[seated.id] += h.points
                    biggest[seated.id] = max(biggest[seated.id], h.points)
                    if kind == HAND_SELF_DRAW_WIN:
                        sd_win[seated.id] += 1
                    else:
                        ron_win[seated.id] += 1
                elif kind == HAND_DEAL_IN:
                    deal_in[seated.id] += 1
                elif kind == HAND_SELF_DRAW_LOSS:
                    sd_lose[seated.id] += 1
        draw_table = hp - decided
        for wind in (1, 2, 3, 4):
            seated = seat_lookup.get((r, t, wind))
            if seated is not None:
                total_hands[seated.id] += hp
                draws[seated.id] += draw_table

    placement = defaultdict(lambda: {1: 0, 2: 0, 3: 0, 4: 0})
    opp_total = defaultdict(float)
    opp_count = defaultdict(int)
    for peers in seats_by_table.values():
        scored_peers = [p for p in peers if getattr(p, rank_field) is not None]
        for p in peers:
            mine = getattr(p, rank_field)
            if mine is not None:
                place = _place_within_table(
                    mine, [getattr(q, rank_field) for q in scored_peers])
                if place <= 4:
                    placement[p.player_id][place] += 1
            for q in peers:
                if q.player_id != p.player_id:
                    opp_total[p.player_id] += field_total.get(q.player_id, 0.0)
                    opp_count[p.player_id] += 1

    # Standings drive rank, totals and per-round scores, masked exactly like the
    # public leaderboard (mirrors desktop's scores_per_player_rows call).
    standings = player_standings(
        tenant, tournament, full_view=full_view, seats=seats,
    )

    def _rate(n, d):
        return round(100 * n / d, 1) if d else None

    rows = []
    for s in standings:
        pid = s['player_id']
        th = total_hands.get(pid, 0)
        wins = sd_win.get(pid, 0) + ron_win.get(pid, 0)
        rows.append({
            'rank': s['pos'],
            'name': s['name'],
            'country': s.get('country', ''),
            'team': s.get('team', ''),
            'total_tp': s['total']['tp'],
            'total_mp': s['total']['mp'],
            'scores': s.get('scores', []),
            'placement': placement.get(pid, {1: 0, 2: 0, 3: 0, 4: 0}),
            'total_hands': th,
            'wins': wins,
            'sd_win': sd_win.get(pid, 0),
            'ron_win': ron_win.get(pid, 0),
            'sd_win_share_pct': _rate(sd_win.get(pid, 0), wins),
            'sd_rate_pct': _rate(sd_win.get(pid, 0), th),
            'deal_in': deal_in.get(pid, 0),
            'deal_in_pct': _rate(deal_in.get(pid, 0), th),
            'sd_lose': sd_lose.get(pid, 0),
            'sd_lose_pct': _rate(sd_lose.get(pid, 0), th),
            'draws': draws.get(pid, 0),
            'avg_hand_value': round(won_pts.get(pid, 0) / wins, 1) if wins else None,
            'biggest_hand': biggest.get(pid, 0) or None,
            'opp_total': round(opp_total.get(pid, 0.0), 1) if opp_count.get(pid) else None,
            'opp_avg': round(opp_total.get(pid, 0.0) / opp_count[pid], 1) if opp_count.get(pid) else None,
            'opp_count': opp_count.get(pid, 0),
        })

    return {
        'round_max': round_max,
        'rules': tournament.rules,
        'uses_teams': tournament.has_teams,
        'players': rows,
    }


def _validated_tables(tenant, round_max):
    """(round, table) pairs whose score sheet is validated, capped at round_max."""
    return {
        (s.round_nb, s.table_nb)
        for s in ScoreSheet.objects.filter(
            tenant=tenant, validated=True, round_nb__lte=round_max)
    }


def _table_stats_for(seats, hands, valid):
    """Table-completion + deal-in ("From") ratios over the given validated tables.

    A validated sheet stores exactly the hands played, so hands-played is the Hand
    row count and draws are the hands with no winner.
    """
    hand_by_table = _group_by(
        (h for h in hands if (h.round_nb, h.table_nb) in valid),
        key=lambda h: (h.round_nb, h.table_nb),
    )
    hands_played = {rt: len(hand_by_table[rt]) for rt in valid}

    tables_total = len(valid)
    tables_finished = sum(1 for n in hands_played.values() if n == 16)
    avg_hands = round(sum(hands_played.values()) / tables_total, 1) if tables_total else 0

    # Per-player win/luck tallies from every game hand on a validated table. A seat
    # (win_by / win_from wind) is resolved to a player via the seat lookup, the same
    # N+1-avoidance as _hand_item.
    #   deal_ins    — gave the winning tile (a discard win from another seat)
    #   self_draws  — won by self-draw (the "luckiest": no one had to feed them)
    #   sd_victims  — sat through someone else's self-draw (the "unluckiest": paid
    #                 out without dealing in; all three non-winners are victims)
    # Alongside them, the tournament-wide average value of a won hand.
    seat_lookup = {(p.round_nb, p.table_nb, p.wind): p.player for p in seats}
    deal_ins = defaultdict(int)
    self_draws = defaultdict(int)
    sd_victims = defaultdict(int)
    wins = defaultdict(int)
    won_pts = won_count = 0
    for rt, table_hands in hand_by_table.items():
        for h in table_hands:
            if h.is_draw:
                continue
            won_pts += h.points
            won_count += 1
            for wind in (1, 2, 3, 4):
                who = seat_lookup.get((h.round_nb, h.table_nb, wind))
                if who is None:
                    continue
                kind = classify_hand(h, wind)
                if kind in (HAND_SELF_DRAW_WIN, HAND_DISCARD_WIN):
                    wins[who] += 1
                    if kind == HAND_SELF_DRAW_WIN:
                        self_draws[who] += 1
                elif kind == HAND_DEAL_IN:
                    deal_ins[who] += 1
                elif kind == HAND_SELF_DRAW_LOSS:
                    sd_victims[who] += 1

    avg_hand_value = round(won_pts / won_count, 1) if won_count else 0

    # Hands played by a player = the played-count of every validated table they sat at.
    played = defaultdict(int)
    for p in seats:
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

    # Self-draw rate = share of a player's own wins that were self-drawn (denominator
    # is wins, not hands played). For the top card, ties on the rate are broken by the
    # raw self-draw count (a 5/5 outranks a 1/1); for the bottom card, by the win count
    # (0/10 is a more telling "never self-drew" than 0/1).
    sd_rate_items = [
        {'player': player, 'count': self_draws.get(player, 0), 'nb_hands': w,
         'pct': round(100 * self_draws.get(player, 0) / w, 1)}
        for player, w in wins.items() if w > 0
    ]
    sd_win_rate = sorted(sd_rate_items, key=lambda d: (d['pct'], d['count']), reverse=True)[:5]
    sd_win_rate_low = sorted(sd_rate_items, key=lambda d: (d['pct'], -d['nb_hands']))[:5]

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
        'sd_win_rate': sd_win_rate,
        'sd_win_rate_low': sd_win_rate_low,
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

    my_seats = list(Seat.objects.filter(
        tenant=tenant, draw_number=player.draw_number).order_by('round_nb'))
    if not my_seats:
        return []

    rounds_set = {p.round_nb for p in my_seats}
    seats_by_rt = _group_by(
        _attach_players(tenant, list(
            Seat.objects.filter(tenant=tenant, round_nb__in=rounds_set).order_by('wind'))),
        key=lambda p: (p.round_nb, p.table_nb),
    )

    return _rounds_for(my_seats, seats_by_rt, schedule, completed)


def _rounds_for(my_seats, seats_by_rt, schedule, completed):
    def _sched(round_nb):
        # The Nth round-row of the schedule describes round N. Staff edit the
        # schedule freely, so it may hold fewer round-rows than there are seated
        # rounds; fall back to a blank agenda entry rather than an IndexError.
        idx = round_nb - 1
        return schedule[idx] if 0 <= idx < len(schedule) else None
    rows = []
    for p in my_seats:
        entry = _sched(p.round_nb)
        rows.append({
            'table_seats': seats_by_rt[(p.round_nb, p.table_nb)],
            'player_wind': WINDS[p.wind - 1],
            'time': entry.time if entry else '',
            'day': entry.day if entry else '',
            'name': entry.name if entry else '',
            'detailed_hands': (p.round_nb, p.table_nb) in completed,
        })
    return rows


def all_slot_rounds(tenant):
    """player_rounds for every draw slot in the seating chart, keyed by draw_number.

    Cards are printed per draw slot, not per player: a slot may not have an
    assigned player yet, but its seats (and so its rounds and opponents) exist in
    the schedule regardless. Grouping by draw_number instead of player_id gives a
    card for every slot, including the undrawn ones. Returns {draw_number: rounds}.
    """
    schedule = player_schedule(tenant)
    completed = completed_tables(tenant)

    all_seats = _attach_players(tenant, list(
        Seat.objects.filter(tenant=tenant).order_by('round_nb', 'wind')
    ))
    seats_by_rt = _group_by(all_seats, key=lambda p: (p.round_nb, p.table_nb))
    seats_by_draw = _group_by(all_seats, key=lambda p: p.draw_number)

    return {
        draw: _rounds_for(seats, seats_by_rt, schedule, completed)
        for draw, seats in seats_by_draw.items()
    }


def player_extra_stats(tenant, player, tournament, max_round=None):
    """Placement rates and win/loss hand stats for one player.

    `max_round` caps the rounds folded in: a public viewer must not see a withheld
    final round leak into these cards (the per-round score grid in the same modal
    already hides it). None = no cap, for admin/ceremony callers.
    """
    qs = (Seat.objects.filter(tenant=tenant, draw_number=player.draw_number)
          .exclude(unscored_seats_q(tournament)))
    if max_round is not None:
        qs = qs.filter(round_nb__lte=max_round)
    seats = list(qs.order_by('round_nb'))

    counts = _placement_counts(tenant, seats, tournament)
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
    # A validated sheet stores exactly the hands played, so every one counts.
    completed = completed_tables(tenant)
    round_table_seat = {seat.round_nb: (seat.table_nb, seat.wind) for seat in seats}
    hands = list(Hand.objects.filter(
        tenant=tenant,
        round_nb__in=list(round_table_seat.keys()),
    ))

    sd_win = ron_win = deal_in = sd_lose = draw = total_hands = 0
    # Value (points) of the hands this player won, kept per round so the modal can
    # show a per-round average alongside the tournament average.
    won_pts = defaultdict(int)
    won_count = defaultdict(int)
    for h in hands:
        info = round_table_seat.get(h.round_nb)
        if info is None or h.table_nb != info[0]:
            continue
        if (h.round_nb, h.table_nb) not in completed:
            continue
        total_hands += 1
        kind = classify_hand(h, info[1])
        if kind == HAND_DRAW:
            draw += 1
        elif kind == HAND_SELF_DRAW_WIN:
            sd_win += 1
        elif kind == HAND_DISCARD_WIN:
            ron_win += 1
        elif kind == HAND_DEAL_IN:
            deal_in += 1
        elif kind == HAND_SELF_DRAW_LOSS:
            sd_lose += 1
        if kind in (HAND_SELF_DRAW_WIN, HAND_DISCARD_WIN):
            won_pts[h.round_nb] += h.points
            won_count[h.round_nb] += 1

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

    # Share of the player's own wins that came by self-draw (denominator is wins,
    # not hands — distinct from the "Win by self-draw" rate, which is over all hands).
    total_wins = sd_win + ron_win
    sd_win_share_pct = sd_win / total_wins * 100 if total_wins else None

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
        'sd_win': sd_win,
        'total_wins': total_wins,
        'sd_win_share_pct': sd_win_share_pct,
        'hand_value': hand_value,
        'avg_hand_value': avg_hand_value,
        'total_won': total_won,
        'opp_strength': _opponent_strength(tenant, seats, tournament, max_round),
    }


def team_extra_stats(tenant, team_name, tournament, max_round=None):
    """Placement rates and win/loss stats aggregated over all players in a team.

    `max_round` caps the rounds folded in, exactly like `player_extra_stats` —
    public team modals must not leak a withheld final round. None = no cap.
    """
    players = list(Player.objects.filter(tenant=tenant, team=team_name))
    if not players:
        return None

    draw_numbers = [p.draw_number for p in players if p.draw_number is not None]
    qs = (Seat.objects.filter(tenant=tenant, draw_number__in=draw_numbers)
          .exclude(unscored_seats_q(tournament)))
    if max_round is not None:
        qs = qs.filter(round_nb__lte=max_round)
    seats = _attach_players(tenant, list(qs.order_by('round_nb')))

    counts = _placement_counts(tenant, seats, tournament)
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
    for seat in seats:
        round_table_seat.setdefault(seat.player_id, {})[seat.round_nb] = (seat.table_nb, seat.wind)

    hands = list(Hand.objects.filter(
        tenant=tenant,
        round_nb__in=list({p.round_nb for p in seats}),
    ))

    sd_win = ron_win = deal_in = sd_lose = draw = total_hands = 0
    for h in hands:
        if (h.round_nb, h.table_nb) not in completed:
            continue
        for pid, rts_map in round_table_seat.items():
            info = rts_map.get(h.round_nb)
            if info is None or h.table_nb != info[0]:
                continue
            total_hands += 1
            kind = classify_hand(h, info[1])
            if kind == HAND_DRAW:
                draw += 1
            elif kind == HAND_SELF_DRAW_WIN:
                sd_win += 1
            elif kind == HAND_DISCARD_WIN:
                ron_win += 1
            elif kind == HAND_DEAL_IN:
                deal_in += 1
            elif kind == HAND_SELF_DRAW_LOSS:
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


def _placement_counts(tenant, seats, tournament):
    """How often these seats placed 1st/2nd/3rd/4th at their own table.

    A seat's place is its rank within its (round, table): by table points for MCR,
    by minipoints otherwise. Tied seats share a place (1, 1, 3, 4 — standard
    competition ranking), so a tie for 1st counts both as 1st and no round is ever
    dropped from the stats.

    `seats` are this player's/team's seats (already filtered to scored rounds);
    the table peers are fetched per round so every seat can be ranked against its
    own table.
    """
    rank_field = 'tablepoints' if tournament.rules == 'MCR' else 'minipoints'
    table_seats = _group_by(
        Seat.objects.filter(
            tenant=tenant,
            round_nb__in={p.round_nb for p in seats},
            **{f'{rank_field}__isnull': False},
        ),
        key=lambda p: (p.round_nb, p.table_nb),
    )
    counts = {1: 0, 2: 0, 3: 0, 4: 0}
    for seat in seats:
        my_val = getattr(seat, rank_field)
        if my_val is None:
            continue
        peers = table_seats[(seat.round_nb, seat.table_nb)]
        place = _place_within_table(
            my_val, [getattr(p, rank_field) for p in peers])
        if place <= 4:
            counts[place] += 1
    return counts


def _opponent_strength(tenant, seats, tournament, max_round=None):
    """Strength of schedule: total & average full-tournament table points of every
    opponent this player faced (an opponent faced twice counts twice).

    Mirrors _placement_counts: ranks on tablepoints for MCR, minipoints otherwise.
    `max_round` caps the opponents' totals to the rounds this modal is allowed to
    show, so a withheld final round can't leak in.
    """
    if not seats:
        return {'total': 0, 'avg': 0, 'count': 0}
    field = 'tablepoints' if tournament.rules == 'MCR' else 'minipoints'
    # Competitors are identified by draw_number here (a Seat has no player FK; the
    # draw_number maps 1:1 to a Player), so opponent totals are summed per slot.
    totals_qs = Seat.objects.filter(tenant=tenant, **{f'{field}__isnull': False})
    if max_round is not None:
        totals_qs = totals_qs.filter(round_nb__lte=max_round)
    totals = dict(totals_qs.values_list('draw_number').annotate(Sum(field)))

    table_seats = _group_by(
        Seat.objects.filter(
            tenant=tenant, round_nb__in={p.round_nb for p in seats},
        ),
        key=lambda p: (p.round_nb, p.table_nb),
    )
    total = 0.0
    count = 0
    for seat in seats:
        for peer in table_seats[(seat.round_nb, seat.table_nb)]:
            if peer.draw_number == seat.draw_number:
                continue
            total += totals.get(peer.draw_number, 0) or 0
            count += 1
    return {'total': total, 'avg': total / count if count else 0, 'count': count}


def _winners_for_round(seats, hands, round_complete):
    mp_max = _top_by(seats, key=lambda p: p.minipoints)
    empty = {'mp_max': mp_max, 'hand_max': [], 'sd_hand_max': [], 'ron_hand_max': [],
             'sd_win_max': [], 'ron_win_max': [], 'total_win_max': []}
    if not round_complete:
        return empty
    # Pre-resolve player from seats to avoid N+1 in template via Hand.win_by_player().
    seat_lookup = {(p.round_nb, p.table_nb, p.wind): p.player for p in seats}
    game_hands = [h for h in hands if not h.is_draw]
    sd_hands = [h for h in game_hands if h.is_self_draw]
    ron_hands = [h for h in game_hands if not h.is_self_draw]
    def _hand_item(h):
        return {
            'points': h.points,
            'round_nb': h.round_nb,
            'table_nb': h.table_nb,
            'player': seat_lookup.get((h.round_nb, h.table_nb, h.win_by)),
        }
    return {
        'mp_max':        mp_max,
        'hand_max':      [_hand_item(h) for h in _top_by(game_hands, key=lambda h: h.points, exclude_zero=True)],
        'sd_hand_max':   [_hand_item(h) for h in _top_by(sd_hands,  key=lambda h: h.points, exclude_zero=True)],
        'ron_hand_max':  [_hand_item(h) for h in _top_by(ron_hands, key=lambda h: h.points, exclude_zero=True)],
        'sd_win_max':    _top_win_streaks(sd_hands, seat_lookup),
        'ron_win_max':   _top_win_streaks(ron_hands, seat_lookup),
        'total_win_max': _top_win_streaks(game_hands, seat_lookup),
    }


def _top_by(items, key, exclude_zero=False):
    """Items tied for max of key. Returns [] if empty, or if exclude_zero and max is 0."""
    if not items:
        return []
    top = max(key(x) for x in items)
    if exclude_zero and top == 0:
        return []
    return [x for x in items if key(x) == top]


def _top_win_streaks(hands, seat_lookup=None):
    """For each (table, winning-seat) pair, count wins; keep groups tying for max.

    `seat_lookup` maps (round_nb, table_nb, wind) -> Player; when provided it
    resolves the winning player without triggering Hand.win_by_player()'s N+1.
    """
    if not hands:
        return []
    by_seat = defaultdict(list)
    for h in hands:
        by_seat[(h.table_nb, h.win_by)].append(h)
    tables = sorted({h.table_nb for h in hands})
    ordered = [by_seat.get((t, wind), []) for t in tables for wind in (1, 2, 3, 4)]
    max_wins = max(len(g) for g in ordered)
    if max_wins == 0:
        return []
    def _player_of(h):
        if seat_lookup is None:
            return h.win_by_player
        return seat_lookup.get((h.round_nb, h.table_nb, h.win_by))
    return [
        {'nb_win': len(g), 'player': _player_of(g[0]),
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
