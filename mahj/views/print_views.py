import itertools

from django.http import HttpResponse
from django.template import loader

from ..card_themes import effective_card_css
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


# A4, and the margin kept clear of every edge. Consumer printers cannot print to
# the paper edge — the unprintable strip is commonly 4-6mm and can reach 6.35mm
# (a quarter inch) — and a sheet laid out edge-to-edge loses whatever falls in it,
# which on the outer cards is their content. The cards are cut out anyway, so the
# margin costs card size rather than correctness: raising it shrinks every card.
# 7mm clears the quarter-inch worst case and is the most that still fits eight
# rounds on a one-day A7; 8mm costs that round (measured, see cards/sheet.html).
A4_W_MM, A4_H_MM = 210, 297
SHEET_MARGIN_MM = 7

# Sheet geometry per card format. Both sit on a portrait A4 and duplex with
# "flip on long edge", so the back face mirrors the columns of the front.
#   cols/rows -- the grid of cards on one A4 sheet
#   back      -- front slot index to print in each back slot, i.e. the mirroring
#   rotate    -- slots drawn upside-down on both faces, so a sheet cut in half
#                gives cards that read the same way up (kept for a6_portrait
#                exactly as it has always printed)
#   head/foot -- the header and footer partials this format's cards use; a
#                compact format needs its own, not a scaled copy of the A6 ones
# Adding a format = one entry here, its two partials, one CARD_FORMATS entry.
CARD_LAYOUTS = {
    "a6_portrait": dict(
        cols=2, rows=2, back=[1, 0, 3, 2], rotate={0, 1},
        head="mahj/cards/head_a6.html", foot="mahj/cards/foot_a6.html",
        label="A6 portrait", per_sheet_label="4 per sheet",
    ),
    "a7_landscape": dict(
        cols=2, rows=4, back=[1, 0, 3, 2, 5, 4, 7, 6], rotate=set(),
        head="mahj/cards/head_a7.html", foot="mahj/cards/foot_a7.html",
        label="A7 landscape", per_sheet_label="8 per sheet",
    ),
}
DEFAULT_CARD_FORMAT = "a6_portrait"

# The printed size of one card, derived rather than written down: it is whatever a
# sheet's grid leaves once the margin is taken off, and the design page's preview
# frame reads these so it always matches what comes out of the printer.
for _layout in CARD_LAYOUTS.values():
    _layout["card_w"] = round((A4_W_MM - 2 * SHEET_MARGIN_MM) / _layout["cols"], 2)
    _layout["card_h"] = round((A4_H_MM - 2 * SHEET_MARGIN_MM) / _layout["rows"], 2)
del _layout


def _card_rounds(rounds, draw_number):
    """The rounds of one card as plain dicts.

    The card templates get data, not model instances: someone customising a badge
    in a fork edits templates, and should not need to know the ORM or which
    attribute of a Seat is safe to touch. Adding a printed field is one line here.
    """
    return [
        {
            "n": n,
            "day": r["day"],
            "time": r["time"],
            "table": r["table_seats"][0].table_nb if r["table_seats"] else "",
            "wind": r["player_wind"][:1],
            "seats": [
                {"draw_number": s.draw_number,
                 "short_name": s.player_short_name,
                 "is_me": s.draw_number == draw_number}
                for s in r["table_seats"]
            ],
        }
        for n, r in enumerate(rounds, start=1)
    ]


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
    cards = []
    for draw, rounds in sorted(rounds_by_draw.items()):
        player = players_by_draw.get(draw)
        cards.append({
            "draw_number": draw,
            "name": player.full_name if player else f"Player {draw}",
            # A slot with no player shows no country block at all, rather than a
            # blank flag: `flag` empty means "unknown", 'mi' means Independent.
            "flag": _country_flag(player.country) if player else "",
            "has_player": player is not None,
            "rounds": _card_rounds(rounds, draw),
        })

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
                and any(seat["draw_number"] in wanted
                        for rnd in c["rounds"] for seat in rnd["seats"]))
        ]

    layout = CARD_LAYOUTS.get(tournament.card_format, CARD_LAYOUTS[DEFAULT_CARD_FORMAT])

    # ?preview=1 -- what the card-design page shows beside its controls: one
    # card, cropped to the card itself rather than the sheet it prints on, so the
    # design is big enough to judge. Never rotated: the duplex flip is a property
    # of the sheet, and an upside-down preview would just be hard to read.
    preview = request.GET.get('preview', '').lower() in ('1', 'true', 'yes')
    if preview:
        card = cards[0] if cards else None
        sheets = [{"front": [{"card": card, "rotate": False}],
                   "back": [{"card": card, "rotate": False}]}]
    else:
        per_sheet = layout["cols"] * layout["rows"]
        sheets = []
        for start in range(0, max(len(cards), 1), per_sheet):
            chunk = cards[start:start + per_sheet]
            if not chunk:
                break
            # Pad the last sheet so the grid keeps its shape; an empty slot prints blank.
            front = [
                {"card": chunk[i] if i < len(chunk) else None, "rotate": i in layout["rotate"]}
                for i in range(per_sheet)
            ]
            sheets.append({
                "front": front,
                "back": [front[i] for i in layout["back"]],
            })

    template = loader.get_template('mahj/cards/sheet.html')
    return HttpResponse(template.render({
        "sheets": sheets,
        "layout": layout,
        "preview": preview,
        "card_format": tournament.card_format,
        "sheet_margin": SHEET_MARGIN_MM,
        "theme": tournament.card_theme,
        # Table points exist only under MCR (see scoring._common), so a Riichi
        # card drops that pair of columns entirely rather than printing boxes
        # nobody fills -- and the minipoint boxes get the width back.
        "show_table_points": tournament.rules == 'MCR',
        "card_css": effective_card_css(tournament),
        "tournament": tournament,
    }, request))


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
