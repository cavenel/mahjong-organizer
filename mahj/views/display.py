import itertools
import math

import simplejson as json

from django.contrib.auth.decorators import user_passes_test
from django.db.models import Q
from django.http import HttpResponse, HttpResponseNotFound
from django.shortcuts import render
from django.template import loader

from ..models import Player, Position, Schedule, Screen
from ..signals import broadcast_display
from ..scoring import _last_round_reveal
from .ceremony import ceremony_active_payload
from .helpers import get_podium, get_tenant, get_variables, is_display_op
from .scoring import scores_per_player_json, scores_per_table_json, stat_all_rounds


def _spectator_qr_svg(subdomain):
    """Inline SVG QR linking to the public spectator site. Generated locally
    (segno, a pinned dependency) so the projector never depends on an external
    QR service at render time. Empty string if there's no subdomain or segno
    isn't installed on this host."""
    if not subdomain:
        return ''
    try:
        import segno
    except ImportError:
        return ''
    url = f'https://{subdomain}.mahj.ovh'
    return segno.make(url, error='m').svg_inline(scale=4, border=2)


def index(request, screen_id=None):
    variables = get_variables(request)
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''

    # Prize-giving ceremony takes over every screen while it's running,
    # overriding the screen's configured view. Additive — see views/ceremony.py.
    _state, ceremony_payload = ceremony_active_payload(request)
    if ceremony_payload is not None:
        template = loader.get_template('mahj/display_ceremony.html')
        return HttpResponse(template.render({
            'payload_json': json.dumps(ceremony_payload),
            'variables': variables,
            'subdomain': subdomain,
        }, request))

    if screen_id:
        try:
            screen = Screen.objects.filter(tenant=tenant).all().order_by('id')[screen_id - 1]
        except IndexError:
            screen = None
    else:
        screen = None

    if screen:
        view = screen.view
        if view == "" or view == "black" or view == "null":
            template = loader.get_template('mahj/display_black.html')
            return HttpResponse(template.render({'subdomain': subdomain}, request))
        elif view == "scores p. 1":
            return scores_per_player(request, "html", 1)
        elif view == "scores p. 2":
            return scores_per_player(request, "html", 2)
        elif view == "scores all":
            return scores_per_player(request, "html", None)
        elif view == "scores all, total only":
            # During the final podium reveal, the "total only" grid would only
            # show the top row — switch to the page-1 view so the reveal is
            # actually legible. Flip back once the reveal is complete.
            reveal = _last_round_reveal(tenant, variables.nb_rounds)
            if reveal is not None and reveal != 0 and reveal <= 14:
                return scores_per_player(request, "html", 1)
            return scores_per_player_total_only(request)
        elif view == "counter":
            return counter(request)
        elif view == "schedule":
            template = loader.get_template('mahj/display_schedule.html')
            schedule = Schedule.objects.filter(tenant=tenant).order_by('id')
            context = {"schedule": schedule, "variables": variables, "subdomain": subdomain}
            return HttpResponse(template.render(context, request))
        return HttpResponse("")
    else:
        template = loader.get_template('mahj/display_no_screen.html')
        return HttpResponse(template.render({'subdomain': subdomain}, request))


def overview(request):
    template = loader.get_template('mahj/display_overview.html')
    return HttpResponse(template.render({}, request))


def counter(request):
    tenant = get_tenant(request)
    variables = get_variables(request)
    counter_val = variables.counter
    scores_json = scores_per_player_json(request, False)[:36]

    def grouper(n, iterable, fillvalue=None):
        args = [iter(iterable)] * n
        return itertools.zip_longest(*args, fillvalue=fillvalue)

    n = min(12, int(math.ceil(len(scores_json) / 3.)))
    tables = list(grouper(n, scores_json))[:3]

    try:
        nb_rounds = len(scores_json[0]["scores"])
    except (IndexError, KeyError):
        nb_rounds = 0

    template = loader.get_template('mahj/display_counter.html')
    context = {
        'variables': variables,
        'counter': counter_val,
        'tables': tables,
        'rounds': range(1, 1 + nb_rounds),
        'subdomain': tenant.subdomain if tenant else '',
    }
    return HttpResponse(template.render(context, request))


def scores_per_table(request, ext):
    tenant = get_tenant(request)
    variables = get_variables(request)
    scores_json = scores_per_table_json(request)
    all_players = Player.objects.filter(tenant=tenant).order_by('full_name')

    if ext == "json":
        return HttpResponse(json.dumps(scores_json))
    elif ext == "html":
        template = loader.get_template('mahj/scores_per_table.html')
        context = {
            'scores_json': scores_json,
            "players": all_players,
            "variables": variables,
        }
        return HttpResponse(template.render(context, request))
    return HttpResponseNotFound('<h1>Page not found</h1>')


def scores_per_player(request, ext, page_nb=None):
    tenant = get_tenant(request)
    variables = get_variables(request)
    schedule = Schedule.objects.filter(tenant=tenant).order_by('id')
    # Display screens aren't authenticated but still need to show the
    # podium reveal when it's in progress. During the reveal, fall through
    # to admin-style standings (which set the per-row `visible` flag).
    reveal = _last_round_reveal(tenant, variables.nb_rounds)
    in_reveal = reveal is not None and reveal <= 11
    check_final = False if in_reveal else not request.user.is_staff
    scores_json = scores_per_player_json(request, check_final)
    podium = get_podium(scores_json)
    stats = stat_all_rounds(request)
    try:
        nb_rounds = len(scores_json[0]["scores"])
    except (IndexError, KeyError):
        nb_rounds = 0
    nb_pages = math.ceil(len(scores_json) / variables.score_lines)
    if page_nb and len(scores_json) > 11 and scores_json[11]["visible"]:
        min_line = (page_nb - 1) * variables.score_lines
        max_line = page_nb * variables.score_lines
        scores_json = scores_json[min_line:max_line]

    final_val = _last_round_reveal(tenant, variables.nb_rounds) or 0

    if ext == "json":
        return HttpResponse(json.dumps(scores_json))
    elif ext == "html":
        scores_json_groups = list(
            itertools.zip_longest(*([iter(scores_json)] * variables.score_lines), fillvalue=None)
        ) or [[]]
        context = {
            "scores_json_groups": scores_json_groups,
            "rounds": range(1, 1 + nb_rounds),
            "max_round": nb_rounds,
            "stats": stats,
            "final": final_val,
            "podium": podium,
            "page": page_nb,
            "nb_pages": nb_pages,
            "view_name": "scores p. " + str(page_nb) if page_nb else "scores all",
            "variables": variables,
            "schedule": schedule,
            "subdomain": tenant.subdomain if tenant else '',
            "qr_svg": _spectator_qr_svg(tenant.subdomain if tenant else ''),
        }
        return render(request, "mahj/display_scores_per_player_table.html", context)
    elif ext == "tpt":
        context = {
            "scores_json": scores_json,
            "rounds": range(1, 1 + nb_rounds),
            "max_round": nb_rounds,
            "variables": variables,
        }
        return render(request, "mahj/mobile_scores_per_player_list.html", context)
    return HttpResponseNotFound('<h1>Page not found</h1>')


def scores_per_player_total_only(request):
    tenant = get_tenant(request)
    variables = get_variables(request)
    # See scores_per_player for the same rationale: display screens need the
    # reveal progression even without a logged-in user.
    reveal = _last_round_reveal(tenant, variables.nb_rounds)
    in_reveal = reveal is not None and reveal <= 11
    check_final = False if in_reveal else True #not request.user.is_staff
    scores_json = scores_per_player_json(request, check_final)
    try:
        nb_rounds = len(scores_json[0]["scores"])
    except (IndexError, KeyError):
        nb_rounds = 0

    lines_per_col = variables.score_lines
    players_per_page = lines_per_col * 3
    pages = []
    for i in range(0, len(scores_json), players_per_page):
        chunk = scores_json[i:i + players_per_page]
        cols = [chunk[j:j + lines_per_col] for j in range(0, len(chunk), lines_per_col)]
        pages.append(cols)
    if not pages:
        pages = [[]]

    final_val = _last_round_reveal(tenant, variables.nb_rounds) or 0

    context = {
        "pages": pages,
        "rounds": range(1, 1 + nb_rounds),
        "max_round": nb_rounds,
        "final": final_val,
        "view_name": "scores all, total only",
        "variables": variables,
        "subdomain": tenant.subdomain if tenant else '',
        "qr_svg": _spectator_qr_svg(tenant.subdomain if tenant else ''),
    }
    return render(request, "mahj/display_scores_per_player_total_only.html", context)


@user_passes_test(is_display_op)
def update_screen_view(request):
    tenant = get_tenant(request)
    screen = Screen.objects.get(tenant=tenant, id=request.GET.get('id'))
    if request.GET.get('view') == "remove":
        screen.delete()
    else:
        try:
            screen.view = request.GET.get('view')
        except Exception:
            screen.view = "black"
        screen.save()
    broadcast_display(tenant.subdomain, 'screen.update', {'event': 'screen_update'})
    return HttpResponse("")


def check_page(request):
    tenant = get_tenant(request)
    try:
        screen = Screen.objects.filter(tenant=tenant).all()[int(request.GET.get('id')) - 1]
        return HttpResponse(screen.view)
    except (IndexError, TypeError, ValueError):
        return HttpResponse("removed")


def check_round(request):
    tenant = get_tenant(request)
    variables = get_variables(request)

    position_vals = Position.objects.filter(tenant=tenant).filter(Q(tablepoints=None) | Q(minipoints=None))
    round_max = variables.nb_rounds
    for position_val in position_vals:
        round_max = min(round_max, position_val.round_nb - 1)

    return HttpResponse(round_max)


def check_variables(request):
    variables = get_variables(request)
    return HttpResponse(str(variables))
