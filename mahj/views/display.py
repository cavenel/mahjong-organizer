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
from .helpers import get_tenant, get_variables, is_display_op
from .scoring import scores_per_player_json, scores_per_table_json


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
        view = screen.view or "black"
        # Grammar: "black" | "counter" | "schedule" | "scores:<density>:<page>"
        # where density is detailed|totals and page is all|<N>.
        if view in ("", "black", "null"):
            template = loader.get_template('mahj/display_black.html')
            return HttpResponse(template.render({'subdomain': subdomain}, request))
        elif view == "counter":
            return counter(request)
        elif view == "schedule":
            template = loader.get_template('mahj/display_schedule.html')
            schedule = Schedule.objects.filter(tenant=tenant).order_by('id')
            context = {"schedule": schedule, "variables": variables, "subdomain": subdomain}
            return HttpResponse(template.render(context, request))
        elif view.startswith("scores:"):
            parts = view.split(":")
            density = parts[1] if len(parts) > 1 and parts[1] in (DETAILED, TOTALS) else DETAILED
            page = parts[2] if len(parts) > 2 else "all"
            page_nb = int(page) if page.isdigit() else None
            return render_scores(request, density, page_nb)
        # Unknown view → blank screen rather than an empty body.
        template = loader.get_template('mahj/display_black.html')
        return HttpResponse(template.render({'subdomain': subdomain}, request))
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


DETAILED = "detailed"   # one wide table, per-round columns
TOTALS = "totals"       # compact rows, several columns side by side


def _score_columns(rows, columns, score_lines):
    """Split standings rows into `columns` columns of up to `score_lines` rows."""
    return [rows[j:j + score_lines] for j in range(0, len(rows), score_lines)] or [[]]


def render_scores(request, density, page_nb=None):
    """Unified projector standings.

    density: DETAILED (per-round breakdown, one column) or TOTALS (compact,
    `variables.total_columns` columns). page_nb: a 1-based page to pin, or None
    to show every page and rotate. An out-of-range page clamps to the last one.
    The view is identical whether or not the browser is logged in as staff.
    """
    tenant = get_tenant(request)
    variables = get_variables(request)
    columns = 1 if density == DETAILED else max(1, variables.total_columns)
    show_rounds = density == DETAILED
    score_lines = variables.score_lines

    prepublish = _last_round_reveal(tenant, variables.nb_rounds) == 0
    check_final = False if prepublish else True
    scores_json = scores_per_player_json(request, check_final)
    try:
        nb_rounds = len(scores_json[0]["scores"])
    except (IndexError, KeyError):
        nb_rounds = 0

    per_page = score_lines * columns
    nb_pages = max(1, math.ceil(len(scores_json) / per_page)) if scores_json else 1

    if page_nb:
        page_nb = min(max(1, page_nb), nb_pages)   # clamp to a real page
        chunk = scores_json[(page_nb - 1) * per_page:page_nb * per_page]
        pages = [_score_columns(chunk, columns, score_lines)]
    else:
        pages = [
            _score_columns(scores_json[i:i + per_page], columns, score_lines)
            for i in range(0, len(scores_json), per_page)
        ] or [[[]]]

    context = {
        "pages": pages,
        "rounds": range(1, 1 + nb_rounds),
        "show_rounds": show_rounds,
        "col_span": 4 + (nb_rounds if show_rounds else 0),
        "rotate": page_nb is None and len(pages) > 1,
        "page_nb": page_nb,
        "nb_pages": nb_pages,
        "variables": variables,
        "subdomain": tenant.subdomain if tenant else '',
        "qr_svg": _spectator_qr_svg(tenant.subdomain if tenant else ''),
    }
    return render(request, "mahj/display_scores.html", context)


def scores_per_player(request, ext):
    """Public data endpoints: JSON standings and the mobile (tpt) list. The
    projector screens render via render_scores()."""
    tenant = get_tenant(request)
    variables = get_variables(request)
    prepublish = _last_round_reveal(tenant, variables.nb_rounds) == 0
    check_final = False if prepublish else True
    scores_json = scores_per_player_json(request, check_final)

    if ext == "json":
        return HttpResponse(json.dumps(scores_json))
    elif ext == "tpt":
        try:
            nb_rounds = len(scores_json[0]["scores"])
        except (IndexError, KeyError):
            nb_rounds = 0
        return render(request, "mahj/mobile_scores_per_player_list.html", {
            "scores_json": scores_json,
            "rounds": range(1, 1 + nb_rounds),
            "max_round": nb_rounds,
            "variables": variables,
        })
    elif ext == "html":
        return render_scores(request, DETAILED, None)
    return HttpResponseNotFound('<h1>Page not found</h1>')


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
