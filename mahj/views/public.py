"""Public `/` landing page: the tournament desktop view."""
from django.contrib.auth import logout
from django.core.cache import cache
from django.http import HttpResponse, HttpResponseRedirect
from django.template import loader

from ..models import Hand, Position, Schedule
from ..scoring import team_standings
from .helpers import can_access_admin, get_tenant, get_variables
from .scoring import (
    LEADERBOARD_TTL, scores_per_player_json, stat_all_rounds, stat_rounds,
    table_stats, table_stats_rounds, tournament_seating,
)

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

    # The overflow menu (Admin / Log out / Login) varies by role, but the page is
    # cached as one HTML blob, so the cache key must capture every menu variant or
    # the first render leaks its menu to the whole bucket. One token covers both
    # the data view (is_admin → force_all) and the menu: the anonymous crowd all
    # share 'anon' (so nginx still microcaches a single `/` entry), while the few
    # privileged sessions each get their own variant.
    authenticated = request.user.is_authenticated
    user_can_access_admin = can_access_admin(request.user)
    view = ('staff' if is_admin else 'admin' if user_can_access_admin
            else 'user' if authenticated else 'anon')

    # Cache the full rendered HTML — it only changes when scores/variables are written,
    # both of which already call invalidate_leaderboard() which deletes this key.
    html_key = f'desktop_html:{subdomain}:{view}'
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
            'country': s.get('country', ''),
            'pos': s['pos'],
            'pos_se': s.get('pos_se'),
            'total': s['total'],
            'visible': s.get('visible', True),
            'team': s.get('team', ''),
            'scores': scores_with_table,
        })

    uses_teams = any(r['team'] for r in rows)
    team_rows = team_standings(rows, variables, nb_rounds) if uses_teams else []

    schedule_key = f'schedule:{subdomain}'
    schedule = cache.get(schedule_key)
    if schedule is None:
        schedule = list(Schedule.objects.filter(tenant=tenant).order_by('id'))
        cache.set(schedule_key, schedule, 300)
    stat_rounds_data = stat_rounds(request, check_final=check_final, positions=positions, hands=hands)
    stat_all_data = stat_all_rounds(request, check_final=check_final, positions=positions, hands=hands)
    stat_tables_data = table_stats(request, check_final=check_final, positions=positions, hands=hands)
    stat_tables_rounds_data = table_stats_rounds(request, check_final=check_final, positions=positions, hands=hands)
    # Pair each round's winner stats with its table stats so one per-round loop in the
    # template can render both panels (both lists are round-aligned, length round_max).
    stat_rounds_combined = [
        {'winners': w, 'tables': t}
        for w, t in zip(stat_rounds_data, stat_tables_rounds_data)
    ]

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
        'stat_tables': stat_tables_data,
        'stat_rounds_combined': stat_rounds_combined,
        'uses_teams': uses_teams,
        'team_rows': team_rows,
        'user_authenticated': authenticated,
        'user_can_access_admin': user_can_access_admin,
    }
    template = loader.get_template('mahj/desktop.html')
    html = template.render(context, request)
    cache.set(html_key, html, HTML_CACHE_TTL)
    return HttpResponse(html)
