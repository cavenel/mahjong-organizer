import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import json

from django.conf import settings
from django.forms.models import model_to_dict
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template import loader
from django.utils import timezone

from ..models import Schedule, Screen, ScreenMode
from ..signals import broadcast_display
from ..scoring import _final_round_withheld, player_schedule, team_standings
from .admin_views import _mode_breakdowns
from .ceremony import ceremony_active_payload
from .helpers import get_tenant, get_variables, has_role, tenant_role_required
from .scoring import scores_per_player_json


def _spectator_qr_svg(subdomain, public_url=''):
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
    from .helpers import public_site_url
    url = public_site_url(subdomain, public_url)
    return segno.make(url, error='m').svg_inline(scale=3, border=2)


def _venue_clock_ms():
    """Current venue-local wall time as milliseconds-since-midnight.

    The projector clock ticks this forward client-side from a monotonic timer,
    so it stays correct even when the display device's own clock or timezone is
    wrong. Timezone is the deployment-wide ``VENUE_TZ`` (server stores UTC)."""
    try:
        tz = ZoneInfo(settings.VENUE_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo('UTC')
    now = timezone.now().astimezone(tz)
    return ((now.hour * 3600 + now.minute * 60 + now.second) * 1000
            + now.microsecond // 1000)


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
        # Grammar: "black" | "welcome" | "counter" | "announcement" | "schedule"
        # | "scores:<density>:<page>" where density is detailed|totals and page
        # is all|<N>.
        if view in ("", "black", "null"):
            template = loader.get_template('mahj/display_black.html')
            return HttpResponse(template.render({'subdomain': subdomain}, request))
        elif view == "welcome":
            return welcome(request)
        elif view == "counter":
            return counter(request)
        elif view == "announcement":
            return announcement(request)
        elif view == "schedule":
            return _render_schedule(request, tenant, variables, subdomain)
        elif view.startswith("scores:"):
            parts = view.split(":")
            density = parts[1] if len(parts) > 1 and parts[1] in (DETAILED, TOTALS, TEAMS) else DETAILED
            page = parts[2] if len(parts) > 2 else "all"
            page_nb = int(page) if page.isdigit() else None
            return render_scores(request, density, page_nb)
        # Unknown view → blank screen rather than an empty body.
        template = loader.get_template('mahj/display_black.html')
        return HttpResponse(template.render({'subdomain': subdomain}, request))
    else:
        template = loader.get_template('mahj/display_no_screen.html')
        return HttpResponse(template.render({'subdomain': subdomain}, request))


def _render_schedule(request, tenant, variables, subdomain):
    template = loader.get_template('mahj/display_schedule.html')
    schedule = Schedule.objects.filter(tenant=tenant).order_by('id')
    context = {"schedule": schedule, "variables": variables, "subdomain": subdomain}
    return HttpResponse(template.render(context, request))


def overview(request):
    """Grid of live thumbnails for every configured screen. Lays the screens out
    in a square grid (side = ceil(sqrt(n)): 1→1×1, 4→2×2, 6→3×3, 9→3×3, 10→4×4…)
    and lets the browser scale each 1920×1080 iframe to fit its cell. Counts that
    aren't perfect squares leave the trailing cells empty."""
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    screens = Screen.objects.filter(tenant=tenant).order_by('id')
    count = len(screens)
    # A logged-in display operator gets a one-click mode switcher occupying the
    # first grid cell (active mode highlighted); an anonymous projector view
    # stays a clean, control-free grid.
    can_control = has_role(request, 'display_op')
    modes = []
    if can_control:
        modes = _mode_breakdowns(
            ScreenMode.objects.filter(tenant=tenant).order_by('id'), screens)
    show_controls = bool(modes)
    # The mode switcher takes one extra cell, so size the square grid for it too.
    cell_count = count + (1 if show_controls else 0)
    cols = rows = math.ceil(math.sqrt(cell_count)) if cell_count else 1
    mode_cols = math.ceil(math.sqrt(len(modes))) if modes else 1
    template = loader.get_template('mahj/display_overview.html')
    context = {
        'screens': screens,
        'cols': cols,
        'rows': rows,
        'subdomain': subdomain,
        'modes': modes,
        'show_controls': show_controls,
        'mode_cols': mode_cols,
    }
    return HttpResponse(template.render(context, request))


def welcome(request):
    """Static welcome screen: hero logo, tournament name and the welcome message,
    with the tournament's key info (full name, city, period, rules) pinned at the
    bottom. Reloads live on any display event, like the other static screens."""
    tenant = get_tenant(request)
    variables = get_variables(request)
    subdomain = tenant.subdomain if tenant else ''
    template = loader.get_template('mahj/display_welcome.html')
    context = {
        'variables': variables,
        'subdomain': subdomain,
        'qr_svg': _spectator_qr_svg(subdomain, variables.public_url if variables else ''),
    }
    return HttpResponse(template.render(context, request))


def counter(request):
    tenant = get_tenant(request)
    variables = get_variables(request)
    template = loader.get_template('mahj/display_counter.html')
    context = {
        'variables': variables,
        'counter': variables.counter,
        'subdomain': tenant.subdomain if tenant else '',
    }
    return HttpResponse(template.render(context, request))


def announcement(request):
    """Announcement screen: the "On-screen message" variable (`variables.welcome`),
    auto-sized by the template to fill the space between the logo (upper left) and
    the tournament info bar (bottom, mirroring the Welcome screen) as large as it
    can. Reloads live on a screen switch and patches the text in place when the
    message is edited, like the other static screens."""
    tenant = get_tenant(request)
    variables = get_variables(request)
    subdomain = tenant.subdomain if tenant else ''
    template = loader.get_template('mahj/display_announcement.html')
    context = {
        'variables': variables,
        'subdomain': subdomain,
        'qr_svg': _spectator_qr_svg(subdomain, variables.public_url if variables else ''),
    }
    return HttpResponse(template.render(context, request))


DETAILED = "detailed"   # one wide table, per-round columns
TOTALS = "totals"       # compact rows, several columns side by side
TEAMS = "teams"         # individual totals pages, then team totals pages


def _score_columns(rows, columns, score_lines):
    """Split standings rows into `columns` columns of up to `score_lines` rows."""
    return [rows[j:j + score_lines] for j in range(0, len(rows), score_lines)] or [[]]


def _paginate(rows, columns, score_lines, kind, label=''):
    """Split standings `rows` into page dicts, each holding up to
    `columns` × `score_lines` rows. `kind` ('players'|'teams') tells the template
    which header to draw; `label` is an optional on-screen section sub-heading
    ('Individuals'/'Teams' for the teams view, '' otherwise)."""
    per_page = score_lines * columns
    chunks = [rows[i:i + per_page] for i in range(0, len(rows), per_page)] or [[]]
    return [
        {'columns': _score_columns(chunk, columns, score_lines), 'kind': kind, 'label': label}
        for chunk in chunks
    ]


def render_scores(request, density, page_nb=None):
    """Unified projector standings.

    density: DETAILED (per-round breakdown, one column), TOTALS (compact,
    `variables.total_columns` columns) or TEAMS (the totals pages followed by
    team-totals pages). page_nb: a 1-based page to pin, or None to show every page
    and rotate. An out-of-range page clamps to the last one. The view is identical
    whether or not the browser is logged in as staff.
    """
    tenant = get_tenant(request)
    variables = get_variables(request)
    columns = 1 if density == DETAILED else max(1, variables.total_columns)
    show_rounds = density == DETAILED
    # Guard the pagination step: 0/None/negative would make range(..., step) throw
    # (ValueError) and 500 every projector. 0 columns of scores is never intended.
    score_lines = max(1, variables.score_lines or 0)

    prepublish = _final_round_withheld(tenant, variables.nb_rounds) is True
    check_final = False if prepublish else True
    scores_json = scores_per_player_json(request, check_final)
    try:
        nb_rounds = len(scores_json[0]["scores"])
    except (IndexError, KeyError):
        nb_rounds = 0

    # No rounds scored yet: "Scores after round 0" with an all-zero table is
    # meaningless, so fall back to the schedule screen until results exist.
    if nb_rounds == 0 and not prepublish:
        return _render_schedule(request, tenant, variables, tenant.subdomain if tenant else '')

    # Individual pages first; the TEAMS view appends team-totals pages after them
    # (skipped when the tournament has no teams — then TEAMS just reads as TOTALS).
    # Section headings ("Individuals"/"Teams") only appear when both sections are
    # present, so a no-team tournament shows an unlabelled totals view. All pages
    # share one rotation loop in the template.
    team_rows = team_standings(scores_json, variables, nb_rounds) if density == TEAMS else []
    all_pages = _paginate(scores_json, columns, score_lines,
                          'players', 'Individuals' if team_rows else '')
    if team_rows:
        all_pages += _paginate(team_rows, columns, score_lines, 'teams', 'Teams')

    nb_pages = len(all_pages)
    if page_nb:
        page_nb = min(max(1, page_nb), nb_pages)   # clamp to a real page
        pages = [all_pages[page_nb - 1]]
    else:
        pages = all_pages

    # Corner badge with the schedule time of the round about to be played. Only
    # while there is a next round to play (not on/after the final round) and not
    # while withholding for the ceremony. The schedule `time` is free text shown
    # verbatim; skip when there's no matching schedule row or it's blank.
    next_round = nb_rounds + 1
    next_round_time = None
    if not prepublish and next_round <= variables.nb_rounds:
        sched = player_schedule(tenant)
        if next_round - 1 < len(sched):
            next_round_time = sched[next_round - 1].time
    if not next_round_time:
        next_round = None

    context = {
        "pages": pages,
        "rounds": range(1, 1 + nb_rounds),
        "show_rounds": show_rounds,
        "col_span": 4 + (nb_rounds if show_rounds else 0),
        "rotate": page_nb is None and len(pages) > 1,
        "page_nb": page_nb,
        "nb_pages": nb_pages,
        "next_round": next_round,
        "next_round_time": next_round_time,
        "server_clock_ms": _venue_clock_ms(),
        "variables": variables,
        "subdomain": tenant.subdomain if tenant else '',
        "qr_svg": _spectator_qr_svg(tenant.subdomain if tenant else ''),
        # Last round published but withheld for the ceremony: every row is masked,
        # so show a holding message instead of an all-blank table.
        "awaiting_ceremony": prepublish,
    }
    return render(request, "mahj/display_scores.html", context)


def _screen_or_404(tenant, raw_id):
    """The screen a ?id= param names, or 404 on a missing/unknown/non-numeric id
    (a bare .get() would 500). Screens are only ever deleted through the admin's
    remove-last action, which keeps the positional /1, /2… addressing stable."""
    try:
        screen_id = int(raw_id)
    except (TypeError, ValueError):
        raise Http404("No such screen")
    return get_object_or_404(Screen, tenant=tenant, id=screen_id)


@tenant_role_required('display_op')
def update_screen_view(request):
    """Point a screen at a view string (see index() for the grammar). An unknown
    string is stored as-is and renders as a blank screen."""
    tenant = get_tenant(request)
    screen = _screen_or_404(tenant, request.GET.get('id'))
    screen.view = request.GET.get('view') or "black"
    screen.save()
    broadcast_display(tenant.subdomain, 'screen.update', {'event': 'screen_update'})
    return HttpResponse("")


@tenant_role_required('display_op')
def update_screen_name(request):
    """Rename a screen. The name is a friendly label only — screens are still
    addressed positionally (/1, /2, …), so renaming never changes a URL. An empty
    name clears it, falling back to the bare positional label in the UI."""
    tenant = get_tenant(request)
    screen = _screen_or_404(tenant, request.GET.get('id'))
    screen.name = (request.GET.get('name') or '').strip()[:70]
    screen.save()
    broadcast_display(tenant.subdomain, 'screen.update', {'event': 'screen_update'})
    return HttpResponse("")


def check_variables(request):
    """All tournament variables as JSON, for displays that patch live instead of
    reloading (e.g. the counter screen). `logo` is a BinaryField (not JSON-
    serializable; served via its own URL + logo_etag) and `tenant` is the FK —
    both excluded."""
    variables = get_variables(request)
    return JsonResponse(model_to_dict(variables, exclude=['logo', 'tenant']))
