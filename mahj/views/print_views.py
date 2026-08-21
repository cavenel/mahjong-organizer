import itertools

from django.http import HttpResponse
from django.template import loader

from ..models import Player, Seat, Schedule
from ..scoring import _attach_players, _country_flag, pad_scores
from .helpers import get_tenant, get_tournament, is_tenant_admin, tenant_admin_required
from .. import scoring as _scoring
from .scoring import scores_per_player_rows, scores_per_table_grid


def cross_positions(request):
    tenant = get_tenant(request)
    scores = scores_per_table_grid(request)
    # A seat carries its draw slot; the competitor holding it (via draw_number) may
    # not exist yet (draw not made), so guard against a missing .player throughout —
    # an unclaimed slot is shown as "Player N".
    players_by_draw = {
        p.draw_number: p
        for p in Player.objects.filter(tenant=tenant, draw_number__isnull=False)
    }

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
                # Skip cells with no seat: the grid is rectangular, so a partly-built
                # chart (or a table seating fewer than four) leaves {} behind, and
                # cell["seat"] on one of those raised KeyError — a 500 on a public
                # page whenever the chart wasn't complete.
                filled = [c for c in table if "seat" in c]
                for i, cell_a in enumerate(filled):
                    player_a = cell_a["seat"].player
                    if player_a is None or player_a.team not in team_idx:
                        continue
                    ta = team_idx[player_a.team]
                    if cell_a["seat"].wind == 1:
                        cross[ta]["east"] += 1
                    for j, cell_b in enumerate(filled):
                        if i == j:
                            continue
                        player_b = cell_b["seat"].player
                        if player_b is not None and player_b.team in team_idx:
                            cross[ta]["cross"][team_idx[player_b.team]] += 1
    else:
        # Key on the draw slot (always present on a Seat), so the sheet works even
        # before the draw is made. Rows/columns share this order.
        draws = sorted({
            cell["seat"].draw_number
            for round_ in scores for table in round_ for cell in table
            if "seat" in cell
        })
        idx = {d: i for i, d in enumerate(draws)}

        def label(d):
            p = players_by_draw.get(d)
            return p.short_name if p else "Player {0}".format(d)

        cross = [{"player": label(d), "east": 0, "cross": [0] * len(draws)} for d in draws]
        for round_ in scores:
            for table in round_:
                seats = [cell["seat"] for cell in table if "seat" in cell]
                for seat in seats:
                    si = idx[seat.draw_number]
                    if seat.wind == 1:
                        cross[si]["east"] += 1
                    for other in seats:
                        if other.draw_number != seat.draw_number:
                            cross[si]["cross"][idx[other.draw_number]] += 1

    template = loader.get_template('mahj/print_cross_positions.html')
    return HttpResponse(template.render({'cross': cross}, request))


def print_scores(request):
    tournament = get_tournament(request)
    nb_rounds = tournament.nb_rounds
    # Mask to the viewer's privilege, exactly like the public desktop: a tenant
    # admin sees every round, the public sees only published/non-withheld ones.
    # Without this the printable sheet leaked the withheld final during the
    # pre-ceremony suspense window.
    standings = scores_per_player_rows(request, full_view=is_tenant_admin(request))
    # One cell per round column in the header, blank for a round the player sat out.
    for row in standings:
        row['scores'] = pad_scores(row['scores'], nb_rounds)
    template = loader.get_template('mahj/print_scores.html')
    context = {
        'standings': standings,
        'rounds': range(1, 1 + nb_rounds),
        'max_round': nb_rounds,
        'tournament': tournament,
    }
    return HttpResponse(template.render(context, request))


def print_schedule(request):
    tenant = get_tenant(request)
    tournament = get_tournament(request)
    schedule = Schedule.objects.filter(tenant=tenant).order_by('id')
    template = loader.get_template('mahj/print_schedule.html')
    return HttpResponse(template.render({'schedule': schedule, 'tournament': tournament}, request))


def player_cards(request):
    tenant = get_tenant(request)
    tournament = get_tournament(request)

    # One card per draw slot, not per listed player: the seating (and so a slot's
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
                and any(seat.draw_number in wanted
                        for rnd in c["rounds"] for seat in rnd["table_seats"]))
        ]

    def grouper(n, iterable, fillvalue=None):
        args = [iter(iterable)] * n
        return itertools.zip_longest(*args, fillvalue=fillvalue)

    pages = list(grouper(4, cards))

    template = loader.get_template('mahj/print_player_cards.html')
    return HttpResponse(template.render({"pages": pages, 'tournament': tournament}, request))


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


@tenant_admin_required
def table_posters(request):
    tenant = get_tenant(request)
    tournament = get_tournament(request)
    seat_rows = _attach_players(tenant, list(
        Seat.objects.filter(tenant=tenant).order_by('id')))

    round_max = 0
    table_max = 0
    for seat in seat_rows:
        round_max = max(round_max, seat.round_nb)
        table_max = max(table_max, seat.table_nb)
    # Store the Seat itself (not just its player) so the poster can label an
    # unclaimed draw slot "Player <n>" via Seat.player_short_name. Each table
    # carries its own number: the template chunks the round into pages and then
    # into rows, and a number derived from those nested loop counters came out
    # wrong past the first page.
    grid = [
        [{'number': t + 1, 'seats': [None, None, None, None]} for t in range(table_max)]
        for _ in range(round_max)
    ]
    for seat in seat_rows:
        grid[seat.round_nb - 1][seat.table_nb - 1]['seats'][seat.wind - 1] = seat

    schedule = list(Schedule.objects.filter(tenant=tenant, is_round=True).order_by('id'))
    template = loader.get_template('mahj/print_table_posters.html')
    context = {"rounds": zip(grid, schedule), "tournament": tournament}
    return HttpResponse(template.render(context, request))
