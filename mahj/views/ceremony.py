"""Prize-giving ceremony: operator console + display-screen takeover.

The ceremony is an additive layer on top of the normal display screens. A
persistent `CeremonyState` row (one per tenant) holds the current phase/step;
the operator console in `/admin?page=ceremony` mutates it via `ceremony_control`,
which broadcasts a `ceremony.update` over the display websocket. Every screen
reloads (their blind `onmessage -> reload`) into `index()`, which renders the
ceremony slide instead of the screen's configured view while a ceremony is
active. The ceremony page itself applies later updates live without reloading.

Nothing here changes the existing players-only `reveal_level` podium logic; the
final "Publish to everyone" simply reuses the publish path (reveal_level=100).
"""
import simplejson as json

from django.contrib.auth.decorators import user_passes_test
from django.http import HttpResponse, JsonResponse

from ..models import CeremonyState, PublishedRound
from ..scoring import _country_flag, team_standings
from ..signals import broadcast_display, invalidate_leaderboard
from .helpers import get_tenant, get_variables, is_display_op
from .scoring import scores_per_player_json, stat_all_rounds


TOP_N = 16        # players revealed
TOP_TEAMS = 3     # only the prize-winning teams

# European country flag codes (ISO alpha-2, lowercase to match _country_flag's
# output). "Best European" is just the top-ranked player whose flag is in here —
# no continent lookup or extra model field needed.
EUROPE = frozenset({
    'al', 'ad', 'at', 'by', 'be', 'ba', 'bg', 'hr', 'cy', 'cz', 'dk', 'ee',
    'fi', 'fr', 'de', 'gr', 'hu', 'is', 'ie', 'it', 'xk', 'lv', 'li', 'lt',
    'lu', 'mt', 'md', 'mc', 'me', 'nl', 'mk', 'no', 'pl', 'pt', 'ro', 'ru',
    'sm', 'rs', 'sk', 'si', 'es', 'se', 'ch', 'ua', 'gb', 'va',
})

# (overall_winners key, slide title, value unit) — order = order shown in the console.
STAT_META = [
    ('mp_max',        'Highest score in a round',      'points'),
    ('ron_hand_max',  'Biggest hand (won on a discard)', 'points'),
    ('sd_hand_max',   'Biggest self-drawn hand',       'points'),
    ('total_win_max', 'Most wins at one table',        'wins'),
    ('ron_win_max',   'Most discard wins at one table', 'wins'),
    ('sd_win_max',    'Most self-draws at one table',  'wins'),
]


def _total(entry, rules):
    """Primary total for display: tablepoints for MCR, minipoints otherwise."""
    t = entry['total']
    return round(t['tp'], 1) if rules == 'MCR' else t['mp']


def _stat_winners(key, items):
    """Normalise an overall_winners category into [{value, name, flag, round_nb, table_nb}]."""
    winners = []
    for it in items:
        if key == 'mp_max':                       # Position instances
            player, value = it.player, it.minipoints
            round_nb, table_nb = it.round_nb, it.table_nb
        elif key in ('sd_hand_max', 'ron_hand_max'):
            player, value = it['player'], it['pts']
            round_nb, table_nb = it['round_nb'], it['table_nb']
        else:                                     # *_win_max dicts (it['pos'] is a Hand)
            player, value = it['player'], it['nb_win']
            round_nb, table_nb = it['pos'].round_nb, it['pos'].table_nb
        if player is None:
            continue
        winners.append({'value': value, 'name': player.full_name,
                        'flag': _country_flag(player.country),
                        'round_nb': round_nb, 'table_nb': table_nb})
    return winners


def _round_label(winners):
    """Human round/table tag shown under a stat value — only when every tying
    winner comes from the same single (round, table); blank otherwise (or for
    overall stats with no round, e.g. Best European)."""
    spots = {(w['round_nb'], w['table_nb']) for w in winners}
    if len(spots) == 1:
        round_nb, table_nb = next(iter(spots))
        if round_nb:
            return f'Round {round_nb} · Table {table_nb}'
    return ''


def _ceremony_master(request):
    """Full ceremony dataset (top teams, top players, stats) for the console and
    for rendering individual slides. Uses true final standings (force_all)."""
    variables = get_variables(request)
    rules = variables.rules
    rows = scores_per_player_json(request, check_final=False, force_all=True)
    id_to_name = {r['player_id']: r['name'] for r in rows}

    players = [
        {'pos': r['pos'], 'player_id': r['player_id'], 'name': r['name'], 'flag': r['flag'],
         'total': _total(r, rules), 'mp': r['total']['mp']}
        for r in rows[:TOP_N]
    ]
    team_rows = team_standings(rows, variables, variables.nb_rounds)
    teams = [
        {'pos': t['pos'], 'name': t['team'], 'flag': t['flag'],
         'tp': round(t['total']['tp'], 1), 'mp': t['total']['mp'],
         'players': [id_to_name[pid] for pid in t['player_ids'] if pid in id_to_name]}
        for t in team_rows[:TOP_TEAMS]
    ]
    overall = stat_all_rounds(request)
    stats = []
    for key, title, unit in STAT_META:
        winners = _stat_winners(key, overall.get(key) or [])
        if winners:
            stats.append({'key': key, 'title': title, 'unit': unit,
                          'value': winners[0]['value'], 'winners': winners,
                          'round_label': _round_label(winners)})

    # Best European: top-ranked player whose flag is a European code. rows are
    # already sorted by rank, so the first match is the winner.
    best_eu = next((r for r in rows if (r['flag'] or '') in EUROPE), None)
    if best_eu:
        value = _total(best_eu, rules)
        euro = {'key': 'euro', 'title': 'Best European',
                'unit': 'TP' if rules == 'MCR' else 'points', 'value': value,
                'round_label': '',  # overall standing, not a single round
                'winners': [{'value': value, 'name': best_eu['name'],
                             'flag': best_eu['flag']}]}
        # MCR ranks on TP but MP is the tie-breaker — show both on this standing-
        # based stat (Riichi has no TP, so `value` is already the MP total).
        if rules == 'MCR':
            euro['mp'] = best_eu['total']['mp']
        stats.append(euro)

    return {'rules': rules, 'teams': teams, 'players': players,
            'stats': stats, 'uses_teams': bool(teams)}


def _slide_payload(master, state):
    """Build the slide for the current state. Shared by the live broadcast and
    the server-side render (so reconnecting screens rebuild the same view)."""
    phase = state.phase
    payload = {'event': 'ceremony', 'phase': phase, 'step': state.step}

    if phase in ('teams', 'players'):
        entries = master['teams'] if phase == 'teams' else master['players']
        top_count = len(entries)
        step = max(0, min(state.step, top_count))
        # Reveal from the worst rank upward: step k reveals the k highest-pos entries.
        revealed = entries[top_count - step:] if step else []
        payload['title'] = 'Teams' if phase == 'teams' else 'Players'
        payload['rules'] = master['rules']  # so the screen labels the total TP vs MP
        payload['entries'] = sorted(revealed, key=lambda e: e['pos'])  # 1st at top
        payload['current'] = entries[top_count - step] if step else None
        payload['done'] = step >= top_count and top_count > 0
    elif phase == 'stat':
        payload['stat_key'] = state.stat_key
        payload['slide'] = next(
            (s for s in master['stats'] if s['key'] == state.stat_key), None)

    return payload


def ceremony_active_payload(request):
    """(state, payload) when a ceremony is running, else (None, None).
    Called by display.index() to decide whether to take over a screen."""
    tenant = get_tenant(request)
    state = CeremonyState.objects.filter(tenant=tenant).first()
    if not state or state.phase == 'idle':
        return None, None
    return state, _slide_payload(_ceremony_master(request), state)


@user_passes_test(is_display_op)
def ceremony_data(request):
    """Full dataset + current state for the operator console (drives previews
    and button state; lets a reloaded console resume mid-ceremony)."""
    tenant = get_tenant(request)
    master = _ceremony_master(request)
    state = CeremonyState.objects.filter(tenant=tenant).first()
    master['state'] = {
        'phase': state.phase if state else 'idle',
        'step': state.step if state else 0,
        'stat_key': state.stat_key if state else '',
    }
    return JsonResponse(master)


@user_passes_test(is_display_op)
def ceremony_control(request):
    """Mutate ceremony state and broadcast the new slide to all screens.

    Query params:
      action=publish              -> reveal all results publicly and end ceremony
      phase=blank|teams|players|stat [&step=N] [&stat_key=KEY]
    """
    tenant = get_tenant(request)
    variables = get_variables(request)
    subdomain = tenant.subdomain if tenant else ''
    state, _ = CeremonyState.objects.get_or_create(tenant=tenant)

    if request.GET.get('action') == 'publish':
        # Reveal everything to everyone: publish all rounds fully.
        for rnd in range(1, variables.nb_rounds + 1):
            PublishedRound.objects.update_or_create(
                tenant=tenant, round_nb=rnd, defaults={'reveal_level': 100})
        state.phase, state.step, state.stat_key = 'idle', 0, ''
        state.save()
        invalidate_leaderboard(subdomain)  # busts caches + wakes desktop
        broadcast_display(subdomain, 'ceremony.update', {'event': 'ceremony', 'phase': 'idle'})

        # Only now — after the ceremony — push the full final standings to the
        # webhook. During play the last round is published with its result
        # hidden for suspense, so this is the first time complete results leave.
        from ..webhook import fire_webhook, leaderboard_payload
        standings = scores_per_player_json(request, check_final=True)
        fire_webhook(leaderboard_payload('round_published', standings, variables))

        return JsonResponse({'status': 'ok', 'phase': 'idle', 'published': True})

    phase = request.GET.get('phase')
    if phase is not None:
        state.phase = phase
        if phase in ('teams', 'players', 'stat'):
            # stat reveals in two steps: 0 = title only, 1 = value + winners.
            step = request.GET.get('step')
            state.step = int(step) if (step is not None and step.lstrip('-').isdigit()) else 0
        if phase == 'stat':
            state.stat_key = request.GET.get('stat_key', '')
        state.save()

    payload = _slide_payload(_ceremony_master(request), state)
    broadcast_display(subdomain, 'ceremony.update', payload)
    return JsonResponse({'status': 'ok', 'phase': state.phase,
                         'step': state.step, 'stat_key': state.stat_key})
