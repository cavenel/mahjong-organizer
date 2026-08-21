"""Player / team standings and the seating grid, with rank/sort helpers."""
from collections import defaultdict
from itertools import groupby

from ..models import Player, Seat
from ._common import WINDS, _attach_players, _country_flag, seat_is_scored
from .visibility import publish_state, final_withheld_now, _last_complete_round

# Table points are not always representable in binary: a shared table place splits
# its points evenly, so a three-way tie awards (4+2+1)/3 and a four-way 7/4. Summing
# the same scores in a different order then lands on 11.0 for one competitor and
# 11.000000000000002 for another — identical at two decimals, identical to any human
# reading the sheet, but unequal to `==`, which is what decides whether two rows tie.
#
# So round every accumulated total before it is sorted or ranked. 6 dp is far below
# any difference the rules can produce (denominators are 2, 3 and 4, so real totals
# are at least 1/12 apart) and far above the ~1e-15 drift being erased.
TP_DP = 6


def player_standings(tenant, tournament, full_view=False, seats=None):
    """Cumulative player totals with rank evolution across rounds."""
    players = list(Player.objects.filter(tenant=tenant).order_by('id'))
    # Resolve each seat's competitor from the players we already have (the draw
    # lives on Player.draw_number), so no extra query is needed to group seats.
    id_by_draw = {p.draw_number: p.id for p in players if p.draw_number is not None}

    if seats is None:
        seats = list(Seat.objects.filter(tenant=tenant).order_by('round_nb'))

    seats_by_player = defaultdict(list)
    round_max = tournament.nb_rounds
    for seat in seats:
        # "Last fully scored round": the same rule as _last_complete_round and the
        # publish gate (every seat scored), so these surfaces can't drift apart.
        #
        # Once the draw is made every *seat* has a holder — a withdrawal swaps the
        # name and keeps the draw number — so an unheld seat only exists in the
        # pre-draw setup window, where nothing is scored yet. That is not the same
        # as every *player* appearing in every round: a chart needn't seat a given
        # draw number in all of them (a bye in an odd field, a substitute given a
        # fresh number mid-tournament), so a player's score list can have gaps.
        # Anything reading these rows must key on `round_nb`, not on list position —
        # see team_standings below and _desktop_rows in views/public.py.
        if not seat_is_scored(seat, tournament):
            round_max = min(round_max, seat.round_nb - 1)
        pid = getattr(seat, 'player_id', None)
        if pid is None:
            pid = id_by_draw.get(seat.draw_number)
        if pid is None:
            continue  # skip so a stray draw number can't fold scores into a player
        seats_by_player[pid].append(seat)

    last_published, final_withheld = publish_state(tenant, tournament)

    # Public viewers only see rounds that have been explicitly published.
    if not full_view:
        round_max = min(round_max, last_published)

    # End-of-tournament suspense: the last round is published but withheld —
    # prepared for the ceremony yet held back from the public. Public viewers see
    # standings through round_max-1 until it's revealed (the display screen shows a
    # holding message meanwhile; a full-view admin/ceremony bypasses this entirely).
    end_of_tournament = final_withheld_now(round_max, tournament, final_withheld) and not full_view
    if end_of_tournament:
        round_max = max(0, round_max - 1)

    flags = {p.id: _country_flag(p.country) for p in players}
    # One entry per *played* round, so history_pos[i] is the rank after round i+1 and
    # the chart can plot it directly. The round-0 ranking (everyone level at 1 before
    # anything is scored) is computed below because `ranked` is the return value, but
    # it isn't a data point and isn't recorded. team_history_pos in public_modals.py
    # has the same shape.
    history = {p.id: [] for p in players}

    sort_key = _standings_sort_key(tournament)
    rank_key = _standings_rank_key(tournament)

    ranked = []
    for current_round in range(round_max + 1):
        ranked = [
            _cumulative_row(p, seats_by_player[p.id], current_round, flags[p.id])
            for p in players
        ]
        ranked.sort(key=sort_key)
        _assign_ranks(ranked, rank_key, field='pos')
        # National sub-ranking over the home nation's players. Skipped (pos_se
        # stays '') when no home country is configured — a generic install has
        # no baked-in nationality.
        home_country = (tournament.home_country or '').strip().casefold()
        if home_country:
            _assign_ranks([r for r in ranked if r['country'].strip().casefold() == home_country],
                          rank_key, field='pos_se')
        if current_round:
            for r in ranked:
                history[r['player_id']].append(r['pos'])

    for r in ranked:
        r['history_pos'] = history[r['player_id']]

    return ranked


def rounds_played(rows):
    """Highest round any of these standing rows holds a score for, 0 for none.

    The row lists are compact — one entry per round the player actually played —
    so the length of any single row understates the tournament whenever that
    player missed a round. This reads the round numbers instead.
    """
    return max((sc['round_nb'] for r in rows for sc in r.get('scores') or ()),
               default=0)


def pad_scores(scores, nb_rounds):
    """Expand a compact score list into one cell per round 1..nb_rounds.

    Rounds the player didn't play get an empty cell, so a template can iterate
    the result straight against a round-numbered header and every score lands in
    its own column. Rounds beyond nb_rounds are dropped (a masked public view
    asks for fewer rounds than the rows may carry).

    An empty cell is the one with ``mp`` None: minipoints are required of every
    scored seat under every rule set (``seat_is_scored``), so that is what the
    templates test to tell a sat-out round from a score of zero.
    """
    by_round = {sc['round_nb']: sc for sc in scores}
    return [by_round.get(r, {'tp': None, 'mp': None, 'round_nb': r})
            for r in range(1, nb_rounds + 1)]


def team_standings(rows, tournament, nb_rounds):
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
        for sc in s['scores']:
            # An empty cell — a round this player didn't play — carries neither
            # score. Riichi carries only minipoints (it never fills table points at
            # all), so requiring table points here left every Riichi team's per-round
            # cells blank; the totals were summed separately and stayed right.
            if sc.get('tp') is None and sc.get('mp') is None:
                continue
            # Fold into the team's slot for this score's actual round, not its
            # position in the player's compact score list: a player who missed a
            # round (bye / sit-out / substitute) has a shorter list, and keying on
            # position would shift every later score into the wrong team column.
            r_idx = sc['round_nb'] - 1
            if 0 <= r_idx < len(slot['scores']):
                rslot = slot['scores'][r_idx]
                rslot['tp'] = (rslot['tp'] or 0.0) + (sc.get('tp') or 0.0)
                rslot['mp'] = (rslot['mp'] or 0) + (sc.get('mp') or 0)
    for slot in by_team.values():
        # This loop accumulates with `+=`, which has no compensation at all, so the
        # drift TP_DP exists for is reachable here on every Python version.
        slot['total']['tp'] = round(slot['total']['tp'], TP_DP)
        for rslot in slot['scores']:
            if rslot['tp'] is not None:
                rslot['tp'] = round(rslot['tp'], TP_DP)
    team_rows = sorted(by_team.values(), key=_standings_sort_key(tournament))
    _assign_ranks(team_rows, _standings_rank_key(tournament), field='pos')
    for tr in team_rows:
        flags = tr.pop('_flags')
        tr['flag'] = next(iter(flags)) if len(flags) == 1 else ''
    return team_rows


def tournament_seating(tenant, tournament, full_view=False, valid_pairs=None, seats=None):
    """seating grid + player→table lookup. Applies the same end-of-tournament
    masking as player_standings: when the last round is published but withheld for
    the ceremony, public viewers see the final round's seats without MP/TP.
    Public viewers also see MP/TP masked for any unpublished round.
    """
    if seats is None:
        seat_rows = _attach_players(tenant, list(
            Seat.objects.filter(tenant=tenant).order_by('id')
        ))
    else:
        seat_rows = seats
    round_max = max((p.round_nb for p in seat_rows), default=0)
    table_max = max((p.table_nb for p in seat_rows), default=0)

    last_published, final_withheld = publish_state(tenant, tournament)
    last_complete = _last_complete_round(tenant, tournament)
    end_of_tournament = final_withheld_now(last_complete, tournament, final_withheld) and not full_view
    hide_scores_round = last_complete if end_of_tournament else None

    player_table = {(p.player_id, p.round_nb): p.table_nb for p in seat_rows}

    grid = [[[None] * 4 for _ in range(table_max)] for _ in range(round_max)]
    for p in seat_rows:
        grid[p.round_nb - 1][p.table_nb - 1][p.wind - 1] = p

    seating = []
    for r_idx, round_tables in enumerate(grid):
        round_nb = r_idx + 1
        hide_scores = hide_scores_round == round_nb
        # Public viewers see scores only from published rounds.
        if not full_view and round_nb > last_published:
            hide_scores = True
        tables = []
        for t_idx, table in enumerate(round_tables):
            if all(seat is None for seat in table):
                continue
            table_seats = []
            for i in range(4):
                seat = table[i]
                mp = None if hide_scores or seat is None else seat.minipoints
                tp = None if hide_scores or seat is None or seat.tablepoints is None \
                    else float(seat.tablepoints)
                table_seats.append({
                    'wind': WINDS[i],
                    'player': seat.player if seat else None,
                    # Display label for the seat: the real name, or "Player <n>"
                    # for a drawn slot no one holds yet. None only when the seat
                    # itself is absent (an empty slot in a partial table).
                    'name': seat.player_name() if seat else None,
                    'mp': mp,
                    'tp': tp,
                })
            table_nb = t_idx + 1
            has_scores = (not hide_scores) and (
                valid_pairs is not None and (round_nb, table_nb) in valid_pairs
            )
            tables.append({'table_nb': table_nb, 'seats': table_seats, 'has_scores': has_scores})
        seating.append({'round_nb': round_nb, 'tables': tables})

    return seating, player_table


def _standings_sort_key(tournament):
    """Order standing rows best-first by the active rules. MCR ranks on TP (MP
    breaks ties); other rules rank on MP. Used for both players and teams so a
    team's row is ordered exactly like a player's."""
    if tournament.rules == 'MCR':
        return lambda s: (-s['total']['tp'], -s['total']['mp'])
    return lambda s: -s['total']['mp']


def _standings_rank_key(tournament):
    """Tie key for `_assign_ranks`, mirroring `_standings_sort_key` so rows tie
    (share a position) exactly when they're level on every value the active rules
    order by. MCR ranks on TP with MP as the tie-breaker, so a shared position
    needs both equal. Other rules rank on MP alone — equal MP alone ties, and TP
    must not split them, since the sort doesn't order by TP at all (rows level on
    MP keep their input order, so a (MP, TP) key would also assign positions
    non-deterministically)."""
    if tournament.rules == 'MCR':
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


def _cumulative_row(player, all_seats, up_to_round, flag):
    played = [p for p in all_seats if p.round_nb <= up_to_round]
    return {
        # Overwritten by player_standings once every round is ranked; a row built
        # outside it has no history to show.
        'history_pos': [], 'pos': 0, 'pos_se': '',
        'player_id': player.id, 'EMA_ID': player.EMA_ID,
        'first_name': player.first_name, 'last_name': player.last_name.upper(),
        'name': player.full_name, 'country': player.country, 'flag': flag,
        'team': player.team,
        'scores': [{'mp': p.minipoints, 'tp': p.tablepoints, 'round_nb': p.round_nb}
                   for p in played],
        'total': {
            'mp': sum(p.minipoints for p in played),
            # Riichi leaves table points NULL by design (it ranks on minipoints
            # alone), and its rows still carry a tp total so every consumer sees one
            # shape whatever the rules. Tested with `is None` rather than falsiness:
            # 0.0 is a real MCR score — last place — and coercing it to int 0 would
            # flip the total's type.
            'tp': round(sum(0 if p.tablepoints is None else p.tablepoints
                            for p in played), TP_DP),
        },
    }
