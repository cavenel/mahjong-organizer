"""Public `/` landing page: the tournament desktop view."""
from django.contrib.auth import logout
from django.core.cache import cache
from django.http import HttpResponse, HttpResponseRedirect
from django.template import loader

from ..models import Hand, Position, Schedule
from .helpers import get_tenant, get_variables, is_scorer
from .scoring import LEADERBOARD_TTL, scores_per_player_json, stat_all_rounds, stat_rounds, tournament_seating

HTML_CACHE_TTL = LEADERBOARD_TTL  # same TTL as data; also invalidated by signals


def desktop(request):
    tenant = get_tenant(request)
    if tenant is None:
        return HttpResponseRedirect('admin')

    if request.GET.get('logout') == "1":
        logout(request)

    subdomain = tenant.subdomain if tenant else ''
    is_admin = request.user.is_staff
    check_final = not is_admin

    # Cache the full rendered HTML — it only changes when scores/variables are written,
    # both of which already call invalidate_leaderboard() which deletes this key.
    html_key = f'desktop_html:{subdomain}:{is_admin}'
    cached_html = cache.get(html_key)
    if cached_html is not None:
        return HttpResponse(cached_html)

    variables = get_variables(request)
    nb_rounds = variables.nb_rounds

    # Fetch positions and hands once; share between standings, seating, and stats.
    positions = list(
        Position.objects.filter(tenant=tenant).select_related('player').order_by('round_nb')
    )
    hands = list(Hand.objects.filter(tenant=tenant))
    valid_pairs = {(h.round_nb, h.table_nb) for h in hands if h.hand_nb == 17 and h.pts == 1}
    scores_json = scores_per_player_json(request, check_final=True, force_all=is_admin, positions=positions)
    seating, player_table = tournament_seating(
        request, check_final=check_final, force_all=is_admin, valid_pairs=valid_pairs, positions=positions,
    )
    seating_json = [
        {
            'round_nb': r['round_nb'],
            'tables': [
                {
                    'table_nb': t['table_nb'],
                    'has_scores': t.get('has_scores', False),
                    'seats': [
                        {
                            'wind': s['wind'],
                            'mp': s['mp'],
                            'tp': s['tp'],
                            'player': (
                                {'id': s['player'].id, 'full_name': s['player'].full_name}
                                if s['player'] else None
                            ),
                        }
                        for s in t['seats']
                    ],
                }
                for t in r['tables']
            ],
        }
        for r in seating
    ]

    rows = []
    for s in scores_json:
        tables = [player_table.get((s['player_id'], r)) for r in range(1, nb_rounds + 1)]
        scores_with_table = []
        for r_idx, sc in enumerate(s.get('scores', [])):
            scores_with_table.append({
                'tp': sc.get('tp'),
                'mp': sc.get('mp'),
                'table_nb': tables[r_idx] if r_idx < len(tables) else None,
                'round_nb': r_idx + 1,
            })
        rows.append({
            'player_id': s['player_id'],
            'name': s['name'],
            'flag': s['flag'],
            'pos': s['pos'],
            'pos_se': s.get('pos_se'),
            'total': s['total'],
            'visible': s.get('visible', True),
            'team': s.get('team', ''),
            'scores': scores_with_table,
        })

    uses_teams = any(r['team'] for r in rows)
    team_rows = []
    if uses_teams:
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
        sort_key = (lambda x: -x['total']['tp']) if variables.rules == 'MCR' else (lambda x: -x['total']['mp'])
        team_rows = sorted(by_team.values(), key=sort_key)
        for i, tr in enumerate(team_rows, 1):
            tr['pos'] = i
            flags = tr.pop('_flags')
            tr['flag'] = next(iter(flags)) if len(flags) == 1 else ''

    schedule_key = f'schedule:{subdomain}'
    schedule = cache.get(schedule_key)
    if schedule is None:
        schedule = list(Schedule.objects.filter(tenant=tenant).order_by('id'))
        cache.set(schedule_key, schedule, 300)
    stat_rounds_data = stat_rounds(request, check_final=check_final, positions=positions, hands=hands)
    stat_all_data = stat_all_rounds(request, positions=positions, hands=hands)

    context = {
        'variables': variables,
        'rows': rows,
        'rounds': list(range(1, 1 + nb_rounds)),
        'max_round': nb_rounds,
        'seating': seating,
        'seating_json': seating_json,
        'tenant': tenant,
        'schedule': schedule,
        'stat_rounds': stat_rounds_data,
        'stat_all': stat_all_data,
        'uses_teams': uses_teams,
        'team_rows': team_rows,
        'user_is_scorer': is_scorer(request.user),
    }
    template = loader.get_template('mahj/desktop.html')
    html = template.render(context, request)
    cache.set(html_key, html, HTML_CACHE_TTL)
    return HttpResponse(html)
