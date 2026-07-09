"""Player / team standings and the seating grid, with rank/sort helpers."""
from collections import defaultdict
from itertools import groupby

from ..models import Player, Seat
from ._common import WINDS, _attach_players, _country_flag
from .visibility import publish_state, final_withheld_now, _last_complete_round


def player_standings(tenant, tournament, check_final=True, force_all=False, positions=None):
    """Cumulative player totals with rank evolution across rounds."""
    players = list(Player.objects.filter(tenant=tenant).order_by('id'))
    # Resolve each seat's competitor from the players we already have (the draw
    # lives on Player.draw_number), so no extra query is needed to group seats.
    id_by_draw = {p.draw_number: p.id for p in players if p.draw_number is not None}

    if positions is None:
        positions = list(Seat.objects.filter(tenant=tenant).order_by('round_nb'))

    positions_by_player = defaultdict(list)
    round_max = tournament.nb_rounds
    for pos in positions:
        pid = getattr(pos, 'player_id', None)
        if pid is None:
            pid = id_by_draw.get(pos.draw_number)
        if pid is None:
            continue  # an undrawn seat has no scores; ignore it here
        positions_by_player[pid].append(pos)
        if pos.minipoints is None or pos.tablepoints is None:
            round_max = min(round_max, pos.round_nb - 1)

    last_published, final_withheld = publish_state(tenant, tournament)

    # Public viewers only see rounds that have been explicitly published.
    if check_final and not force_all:
        round_max = min(round_max, last_published)

    # End-of-tournament suspense: the last round is published but withheld —
    # prepared for the ceremony yet held back from the public. Public viewers see
    # standings through round_max-1 until it's revealed.
    end_of_tournament = final_withheld_now(round_max, tournament, final_withheld) and not force_all
    if end_of_tournament and check_final:
        round_max = max(0, round_max - 1)

    flags = {p.id: _country_flag(p.country) for p in players}
    history = {p.id: [1] for p in players}

    sort_key = _standings_sort_key(tournament)
    rank_key = _standings_rank_key(tournament)

    ranked = []
    for current_round in range(round_max + 1):
        ranked = [
            _cumulative_row(p, positions_by_player[p.id], current_round, flags[p.id])
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
        for r in ranked:
            history[r['player_id']].append(r['pos'])

    for r in ranked:
        r['history_pos'] = history[r['player_id']]

    # Admin/display viewers (check_final=False) get the full standings, but every
    # row is masked while the final round is withheld. The reveal animation is the
    # ceremony page's job, so these rows stay hidden until the results are revealed.
    if end_of_tournament and not check_final:
        for r in ranked:
            r['visible'] = False

    return ranked


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
        for r_idx, sc in enumerate(s['scores']):
            if r_idx < len(slot['scores']) and sc.get('tp') is not None:
                rslot = slot['scores'][r_idx]
                rslot['tp'] = (rslot['tp'] or 0) + sc['tp']
                rslot['mp'] = (rslot['mp'] or 0) + (sc.get('mp') or 0)
    team_rows = sorted(by_team.values(), key=_standings_sort_key(tournament))
    _assign_ranks(team_rows, _standings_rank_key(tournament), field='pos')
    for tr in team_rows:
        flags = tr.pop('_flags')
        tr['flag'] = next(iter(flags)) if len(flags) == 1 else ''
    return team_rows


def tournament_seating(tenant, tournament, check_final=True, force_all=False, valid_pairs=None, positions=None):
    """seating grid + player→table lookup. Applies the same end-of-tournament
    masking as player_standings: when the last round is published but withheld for
    the ceremony, check_final viewers see the final round's seats without MP/TP.
    Public viewers also see MP/TP masked for any unpublished round.
    """
    if positions is None:
        position_vals = _attach_players(tenant, list(
            Seat.objects.filter(tenant=tenant).order_by('id')
        ))
    else:
        position_vals = positions
    round_max = max((p.round_nb for p in position_vals), default=0)
    table_max = max((p.table_nb for p in position_vals), default=0)

    last_published, final_withheld = publish_state(tenant, tournament)
    last_complete = _last_complete_round(tenant, tournament)
    end_of_tournament = final_withheld_now(last_complete, tournament, final_withheld) and not force_all
    hide_scores_round = last_complete if (end_of_tournament and check_final) else None

    player_table = {(p.player_id, p.round_nb): p.table_nb for p in position_vals}

    grid = [[[None] * 4 for _ in range(table_max)] for _ in range(round_max)]
    for p in position_vals:
        grid[p.round_nb - 1][p.table_nb - 1][p.wind - 1] = p

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
                    # Display label for the seat: the real name, or "Player <n>"
                    # for a drawn slot no one holds yet. None only when the seat
                    # itself is absent (an empty slot in a partial table).
                    'name': pos.player_name() if pos else None,
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
