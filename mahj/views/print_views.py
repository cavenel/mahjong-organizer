import itertools

from django.contrib.auth.decorators import user_passes_test
from django.http import HttpResponse
from django.template import loader

from ..models import Player, Seat, Schedule
from ..scoring import _attach_players, _country_flag
from .helpers import get_tenant, get_variables
from .. import scoring as _scoring
from .scoring import scores_per_player_json, scores_per_table_json


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
                    if pos_a["position"].wind == 1:
                        cross[team_idx[team_a]]["east"] += 1
                    for j, pos_b in enumerate(table):
                        if i == j:
                            continue
                        team_b = pos_b["position"].player.team
                        if team_b in team_idx:
                            cross[team_idx[team_a]]["cross"][team_idx[team_b]] += 1
    else:
        cross = []
        for player in Player.objects.filter(tenant=tenant).order_by('draw_number'):
            cross.append({"player": player.first_name, "east": 0, "cross": []})
            for _ in Player.objects.filter(tenant=tenant).order_by('draw_number'):
                cross[-1]["cross"].append(0)

        for round_ in scores:
            for table in round_:
                players = [position["position"].player for position in table]
                for position in table:
                    for player in players:
                        if player.draw_number != position["position"].player.draw_number:
                            cross[position["position"].player.draw_number - 1]["cross"][player.draw_number - 1] += 1
                    if position["position"].wind == 1:
                        cross[position["position"].player.draw_number - 1]["east"] += 1

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

    # One card per draw slot, not per roster player: the seating (and so a slot's
    # rounds and opponents) exists before the draw is made, so an undrawn slot
    # still gets a usable card, labelled "Player <n>" until a player is assigned.
    players_by_draw = {
        p.draw_number: p
        for p in Player.objects.filter(tenant=tenant, draw_number__isnull=False)
    }
    # rounds for every slot from a constant number of queries, and _country_flag is
    # lru_cached so the ~10 distinct countries are resolved once each, not per card.
    rounds_by_draw = _scoring.all_slot_rounds(tenant)
    cards = [
        {
            "draw_number": draw,
            "player": players_by_draw.get(draw),
            "rounds": rounds,
            "flag": _country_flag(players_by_draw[draw].country) if draw in players_by_draw else "",
        }
        for draw, rounds in sorted(rounds_by_draw.items())
    ]

    # ?players=1,2,3 keeps only those draw slots plus anyone who shares a table
    # with them in some round (their opponents), so a card is printed for every
    # slot relevant to the requested draw numbers. ?main=true drops the
    # opponents and prints only the requested slots' own badges.
    wanted = {
        int(pid) for pid in request.GET.get('players', '').split(',') if pid.strip().isdigit()
    }
    main_only = request.GET.get('main', '').lower() in ('1', 'true', 'yes')
    if wanted:
        cards = [
            c for c in cards
            if c["draw_number"] in wanted
            or (not main_only
                and any(pos.draw_number in wanted
                        for rnd in c["rounds"] for pos in rnd["other_pos"]))
        ]

    def grouper(n, iterable, fillvalue=None):
        args = [iter(iterable)] * n
        return itertools.zip_longest(*args, fillvalue=fillvalue)

    pages = list(grouper(4, cards))

    template = loader.get_template('mahj/print_player_cards.html')
    return HttpResponse(template.render({"pages": pages, 'variables': variables}, request))


def player_names(request):
    tenant = get_tenant(request)
    players = Player.objects.filter(tenant=tenant).all()
    template = loader.get_template('mahj/print_player_names.html')
    return HttpResponse(template.render({"names": players}, request))


def team_names(request):
    tenant = get_tenant(request)
    teams = sorted(
        Player.objects.filter(tenant=tenant)
                      .exclude(team='')
                      .values_list('team', flat=True)
                      .distinct()
    )
    template = loader.get_template('mahj/print_player_names.html')
    return HttpResponse(template.render({"names": teams}, request))


@user_passes_test(lambda u: u.is_staff)
def table_posters(request):
    tenant = get_tenant(request)
    variables = get_variables(request)
    position_vals = _attach_players(tenant, list(
        Seat.objects.filter(tenant=tenant).order_by('id')))

    round_max = 0
    table_max = 0
    for position_val in position_vals:
        round_max = max(round_max, position_val.round_nb)
        table_max = max(table_max, position_val.table_nb)
    # Store the Seat itself (not just its player) so the poster can label an
    # unclaimed draw slot "Player <n>" via Seat.player_short_name.
    positions = [[[None, None, None, None] for _ in range(table_max)] for _ in range(round_max)]
    for position_val in position_vals:
        positions[position_val.round_nb - 1][position_val.table_nb - 1][position_val.wind - 1] = position_val

    schedule = list(Schedule.objects.filter(tenant=tenant, is_round=True).order_by('id'))
    template = loader.get_template('mahj/print_table_posters.html')
    context = {"rounds": zip(positions, schedule), "variables": variables}
    return HttpResponse(template.render(context, request))
