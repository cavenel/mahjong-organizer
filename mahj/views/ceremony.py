"""Prize-giving ceremony: operator console + display-screen takeover.

The ceremony is an additive layer on top of the normal display screens. A
persistent `CeremonyState` row (one per tenant) holds the current phase/step;
the operator console in `/admin?page=ceremony` mutates it via `ceremony_control`,
which broadcasts a `ceremony.update` over the display websocket. Every screen
reloads (their blind `onmessage -> reload`) into `index()`, which renders the
ceremony slide instead of the screen's configured view while a ceremony is
active. The ceremony page itself applies later updates live without reloading.

Nothing here changes the existing players-only withheld-podium logic; the final
"Publish to everyone" simply reuses the publish path (withheld=False).
"""
import json

from django.http import (
    HttpResponse, HttpResponseForbidden, HttpResponseNotAllowed, JsonResponse,
)
from django.template.defaultfilters import floatformat

from ..models import CeremonyState, PublishedRound, Seat
from ..scoring import (
    _country_flag, _final_round_withheld, team_standings, unscored_seats_q,
)
from ..signals import (
    broadcast_display, broadcast_publish_state, invalidate_leaderboard,
)
from .helpers import get_tenant, get_tournament, has_role, tenant_role_required
from .score_entry import _published_rounds
from .scoring import scores_per_player_rows, stat_all_rounds


TOP_N = 16        # players revealed
TOP_TEAMS = 3     # only the prize-winning teams

# European country flag codes (ISO alpha-2, lowercase to match _country_flag's
# output). "Best European" is just the top-ranked player whose flag is in here —
# no continent lookup or extra model field needed.
EUROPE = frozenset({
    'al', 'ad', 'at', 'by', 'be', 'ba', 'bg', 'hr', 'cy', 'cz', 'dk', 'ee',
    'fi', 'fr', 'de', 'gr', 'hu', 'is', 'ie', 'it', 'xk', 'lv', 'li', 'lt',
    'lu', 'mt', 'md', 'mc', 'me', 'nl', 'mk', 'no', 'pl', 'pt', 'ro', 'ru',
    'sm', 'rs', 'sk', 'si', 'es', 'se', 'ch', 'tr', 'ua', 'gb', 'va',
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
    """Primary total for display: tablepoints for MCR, minipoints otherwise.
    TP is formatted like the rest of the app (`floatformat:-2`): at most two
    decimals, trailing zeros and a bare `.0` dropped (35.0→"35", 13.333→"13.33").
    """
    t = entry['total']
    return floatformat(t['tp'], -2) if rules == 'MCR' else t['mp']


def _stat_winners(key, items):
    """Normalise an overall_winners category into [{value, name, flag, round_nb, table_nb}].

    `mp_max` holds Seat rows; every other category holds dicts that carry
    round_nb/table_nb plus a single value field — `nb_win` for the win-streak
    categories, `points` for the biggest-hand ones.
    """
    winners = []
    for it in items:
        if key == 'mp_max':
            player, value = it.player, it.minipoints
            round_nb, table_nb = it.round_nb, it.table_nb
        else:
            player = it['player']
            value = it['nb_win'] if key.endswith('_win_max') else it['points']
            round_nb, table_nb = it['round_nb'], it['table_nb']
        if player is None:
            continue
        winners.append({'value': value, 'name': player.full_name,
                        'flag': _country_flag(player.country),
                        'country': player.country,
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
    for rendering individual slides. Uses true final standings (full_view)."""
    tournament = get_tournament(request)
    rules = tournament.rules
    rows = scores_per_player_rows(request, full_view=True)
    id_to_name = {r['player_id']: r['name'] for r in rows}

    players = [
        {'pos': r['pos'], 'player_id': r['player_id'], 'name': r['name'], 'flag': r['flag'],
         'total': _total(r, rules), 'mp': r['total']['mp']}
        for r in rows[:TOP_N]
    ]
    team_rows = team_standings(rows, tournament, tournament.nb_rounds)
    teams = [
        {'pos': t['pos'], 'name': t['team'], 'flag': t['flag'],
         'tp': floatformat(t['total']['tp'], -2), 'mp': t['total']['mp'],
         'players': [id_to_name[pid] for pid in t['player_ids'] if pid in id_to_name]}
        for t in team_rows[:TOP_TEAMS]
    ]
    overall = stat_all_rounds(request, full_view=True)
    stats = []

    # Best European: top-ranked player whose flag is a European code. rows are
    # already sorted by rank, so the first match is the winner. Shown first.
    best_eu = next((r for r in rows if (r['flag'] or '') in EUROPE), None)
    if best_eu:
        value = _total(best_eu, rules)
        euro = {'key': 'euro', 'title': 'Best European',
                'unit': 'TP' if rules == 'MCR' else 'points', 'value': value,
                'round_label': '',  # overall standing, not a single round
                'winners': [{'value': value, 'name': best_eu['name'],
                             'flag': best_eu['flag'],
                             'country': best_eu['country']}]}
        # MCR ranks on TP but MP is the tie-breaker — show both on this standing-
        # based stat (Riichi has no TP, so `value` is already the MP total).
        if rules == 'MCR':
            euro['mp'] = best_eu['total']['mp']
        stats.append(euro)

    for key, title, unit in STAT_META:
        winners = _stat_winners(key, overall.get(key) or [])
        if winners:
            stats.append({'key': key, 'title': title, 'unit': unit,
                          'value': winners[0]['value'], 'winners': winners,
                          'round_label': _round_label(winners)})

    return {'rules': rules, 'teams': teams, 'players': players,
            'stats': stats, 'uses_teams': tournament.has_teams}


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
        slide = next(
            (s for s in master['stats'] if s['key'] == state.stat_key), None)
        if slide is not None and state.step < 1:
            # Step 0 is the suspense/title-only slide. The client hides the
            # winner until step 1, but the *payload* would still carry it — a
            # spectator could read the result from the socket frame or page
            # source seconds early. Strip the reveal fields so the title-only
            # slide contains only the title.
            slide = {k: v for k, v in slide.items()
                     if k not in ('winners', 'value', 'mp', 'round_label')}
        payload['slide'] = slide

    return payload


def ceremony_active_payload(request):
    """(state, payload) when a ceremony is running, else (None, None).
    Called by display.index() to decide whether to take over a screen."""
    tenant = get_tenant(request)
    state = CeremonyState.objects.filter(tenant=tenant).first()
    if not state or state.phase == 'idle':
        return None, None
    return state, _slide_payload(_ceremony_master(request), state)


@tenant_role_required('display_op')
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
    # True while the final round is published but withheld for the ceremony. The
    # console warns before "End — back to screens" in this state: ending without
    # publishing strands every screen on the "waiting for the ceremony" slide.
    master['final_withheld'] = _final_round_withheld(
        tenant, get_tournament(request).nb_rounds) is True
    return JsonResponse(master)


# The phases the console can drive the ceremony into. Anything else is rejected
# so a crafted request can't strand every screen on an unknown/empty slide.
VALID_PHASES = frozenset({'idle', 'blank', 'teams', 'players', 'stat'})


@tenant_role_required('display_op', 'publisher')
def ceremony_control(request):
    """Mutate ceremony state and broadcast the new slide to all screens.

    POST only (it mutates state and publishes results). Params (query string):
      action=publish              -> reveal all results publicly and end ceremony
                                     (publisher role)
      phase=idle|blank|teams|players|stat [&step=N] [&stat_key=KEY]
                                  (display_op role)
    """
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    tenant = get_tenant(request)
    tournament = get_tournament(request)
    subdomain = tenant.subdomain if tenant else ''

    is_publish = request.GET.get('action') == 'publish'
    # Two jobs behind one endpoint. Revealing results is a publish and takes the
    # publisher role; driving the slides is display-operator work. The decorator
    # admits either, so each branch names the one it needs — a publisher must not
    # inherit the slide controls, nor an operator the reveal. Checked before the
    # get_or_create below, so a refused request writes nothing.
    if not has_role(request, 'publisher' if is_publish else 'display_op'):
        return HttpResponseForbidden('forbidden')

    state, _ = CeremonyState.objects.get_or_create(tenant=tenant)

    if is_publish:
        # Reveal what is already published: the withheld final round is the whole
        # point of the ceremony.
        PublishedRound.objects.filter(tenant=tenant, withheld=True).update(withheld=False)
        # Then publish any round that is complete but was never published, under
        # the same rule `set_round_published` enforces (`unscored_seats_q` is the
        # ORM half of `seat_is_scored`). Incomplete rounds are skipped rather than
        # published: a published round locks score entry, so publishing
        # 1..nb_rounds unconditionally froze every unscored round, recoverable only
        # by a publisher unpublishing it again.
        for rnd in range(1, tournament.nb_rounds + 1):
            seats = Seat.objects.filter(tenant=tenant, round_nb=rnd)
            if not seats.exists() or seats.filter(unscored_seats_q(tournament)).exists():
                # Stop rather than skip: set_round_published refuses to publish a
                # round while an earlier one is unpublished, so carrying on past a
                # gap would leave a state the normal path would have rejected.
                break
            PublishedRound.objects.get_or_create(
                tenant=tenant, round_nb=rnd, defaults={'withheld': False})
        state.phase, state.step, state.stat_key = 'idle', 0, ''
        state.save()
        invalidate_leaderboard(subdomain)  # busts caches + wakes desktop
        broadcast_display(subdomain, 'ceremony.update', {'event': 'ceremony', 'phase': 'idle'})
        # Keep the scorer pages' publish toggles in step, as set_round_published does.
        broadcast_publish_state(subdomain, {'published_rounds': _published_rounds(tenant)})

        # Only now — after the ceremony — push the full final standings to the
        # static spectator site. During play the last round is published with its
        # result hidden for suspense, so this is the first time complete results
        # leave the venue.
        from ..publish.trigger import fire_static_export
        fire_static_export(subdomain)

        return JsonResponse({'status': 'ok', 'phase': 'idle', 'published': True})

    phase = request.GET.get('phase')
    if phase is not None:
        if phase not in VALID_PHASES:
            return HttpResponse(f"Unknown phase: {phase}", status=400)
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
