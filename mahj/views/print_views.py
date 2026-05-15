import itertools

import pycountry

from django.contrib.auth.decorators import user_passes_test
from django.http import HttpResponse
from django.template import loader

from ..models import Player, Player_data, Position, Schedule
from .helpers import get_tenant, get_variables
from .scoring import player_rounds_json, scores_per_player_json, scores_per_table_json

def _country_flag(country):
    if country == "Independent":
        return 'mi'
    try:
        name = country.replace('The ', '').strip()
        match = pycountry.countries.get(name=name)
        if match is None:
            results = pycountry.countries.search_fuzzy(name)
            match = results[0] if results else None
        return match.alpha_2.lower() if match else ''
    except Exception:
        return ''

def cross_positions(request):
    tenant = get_tenant(request)
    scores = scores_per_table_json(request)

    if request.GET.get('per_team'):
        teams = sorted(
            Player.objects.filter(tenant=tenant)
                          .exclude(team='')
                          .values_list('team', flat=True)
                          .distinct()
        )
        team_idx = {t: i for i, t in enumerate(teams)}
        cross = [{"player": t, "east": 0, "cross": [0] * len(teams)} for t in teams]
        for round_ in scores:
            for table in round_:
                for i, pos_a in enumerate(table):
                    team_a = pos_a["position"].player.team
                    if team_a not in team_idx:
                        continue
                    if pos_a["position"].position == 1:
                        cross[team_idx[team_a]]["east"] += 1
                    for j, pos_b in enumerate(table):
                        if i == j:
                            continue
                        team_b = pos_b["position"].player.team
                        if team_b in team_idx:
                            cross[team_idx[team_a]]["cross"][team_idx[team_b]] += 1
    else:
        cross = []
        for player in Player.objects.filter(tenant=tenant).order_by('rand_id'):
            cross.append({"player": player.first_name, "east": 0, "cross": []})
            for _ in Player.objects.filter(tenant=tenant).order_by('rand_id'):
                cross[-1]["cross"].append(0)

        for round_ in scores:
            for table in round_:
                players = [position["position"].player for position in table]
                for position in table:
                    for player in players:
                        if player.rand_id != position["position"].player.rand_id:
                            cross[position["position"].player.rand_id - 1]["cross"][player.rand_id - 1] += 1
                    if position["position"].position == 1:
                        cross[position["position"].player.rand_id - 1]["east"] += 1

    template = loader.get_template('mahj/print_cross_positions.html')
    return HttpResponse(template.render({'cross': cross}, request))


def print_scores(request):
    variables = get_variables(request)
    nb_rounds = variables.nb_rounds
    scores_json = scores_per_player_json(request, force_all=True)
    template = loader.get_template('mahj/print_scores.html')
    context = {
        'scores_json': scores_json,
        'rounds': range(1, 1 + nb_rounds),
        'max_round': nb_rounds,
        'variables': variables,
    }
    return HttpResponse(template.render(context, request))


def print_schedule(request):
    tenant = get_tenant(request)
    variables = get_variables(request)
    schedule = Schedule.objects.filter(tenant=tenant).order_by('id')
    template = loader.get_template('mahj/print_schedule.html')
    return HttpResponse(template.render({'schedule': schedule, 'variables': variables}, request))


def player_cards(request):
    tenant = get_tenant(request)
    variables = get_variables(request)
    players = Player.objects.filter(tenant=tenant).all()
    flags = {}
    for p in players:
        flags[p] = _country_flag(p.country)

    player_rounds = [
        {"player": p, "rounds": player_rounds_json(request, p.id), "flag": flags[p]}
        for p in players
    ]

    def grouper(n, iterable, fillvalue=None):
        args = [iter(iterable)] * n
        return itertools.zip_longest(*args, fillvalue=fillvalue)

    pages = list(grouper(4, player_rounds))

    template = loader.get_template('mahj/print_player_cards.html')
    return HttpResponse(template.render({"pages": pages, 'variables': variables}, request))


@user_passes_test(lambda u: u.is_staff)
def player_names(request):
    tenant = get_tenant(request)
    players = Player_data.objects.filter(tenant=tenant).all()
    template = loader.get_template('mahj/print_player_names.html')
    return HttpResponse(template.render({"players": players}, request))


@user_passes_test(lambda u: u.is_staff)
def table_posters(request):
    tenant = get_tenant(request)
    variables = get_variables(request)
    position_vals = Position.objects.filter(tenant=tenant).order_by('id')

    round_max = 0
    table_max = 0
    for position_val in position_vals:
        round_max = max(round_max, position_val.round_nb)
        table_max = max(table_max, position_val.table_nb)
    positions = [[[None, None, None, None] for _ in range(table_max)] for _ in range(round_max)]
    for position_val in position_vals:
        positions[position_val.round_nb - 1][position_val.table_nb - 1][position_val.position - 1] = position_val.player

    schedule = Schedule.objects.filter(tenant=tenant).order_by('id')
    schedule = [s for s in schedule if "Round" in s.name or "Session" in s.name]
    template = loader.get_template('mahj/print_table_posters.html')
    context = {"rounds": zip(positions, schedule), "variables": variables}
    return HttpResponse(template.render(context, request))
