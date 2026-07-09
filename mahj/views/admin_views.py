import hashlib
import io
import os
import time
import traceback
from collections import defaultdict
from datetime import datetime

import json
from openpyxl import Workbook, load_workbook
from unidecode import unidecode

from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.decorators import user_passes_test
from django.contrib.auth.models import User
from django.core.files.storage import FileSystemStorage
from django.db import transaction
from django.db.models import F
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, HttpResponseRedirect, JsonResponse
from django.template import loader

from ..models import CeremonyState, Hand, Player, ScoreSheet, Seat, PublishedRound, Schedule, Screen, ScreenMode
from ..signals import broadcast_display, broadcast_publish_state, invalidate_leaderboard
from .helpers import BASE_DIR, can_access_admin, get_counter, get_tenant, get_variables, is_display_op, is_publisher, is_scorer, is_scorer_or_display_op, player_statistics, set_counter
from .print_views import _country_flag
from .user_admin import ROLE_GROUPS, reauth_ok
from .restore_admin import list_backups
from .scoring import (
    scores_per_player_json,
    scores_per_table_json,
    stat_all_rounds,
    stat_rounds,
)


# Friendly labels for the editable tournament variables, matching the field
# labels on the display admin page, so a rejected save names the field the way
# the operator sees it ("On-screen message", not "welcome").
_VARIABLE_LABELS = {
    "welcome": "On-screen message",
    "title": "Title",
    "fullname": "Full tournament name",
    "city": "City",
    "period": "Period",
    "rules": "Rules",
    "home_country": "Home nation",
    "countrycourt": "Federation code",
}


def _name_is_round(name):
    """Guess whether an imported schedule row is a playing round from its name.

    Used only at import time to seed ``Schedule.is_round`` (the Excel template
    carries no such column); staff can correct it afterwards in the settings UI.
    """
    name = name or ""
    return "round" in name.lower() or "session" in name.lower()


def _pretty_view(view):
    """Human label for a stored screen view string. Mirrors the prettyView()
    used client-side on the display admin page, so a mode's saved views read the
    same whether rendered by Django or refreshed live by JS.
    Grammar: "black" | "welcome" | "counter" | "announcement" | "schedule"
    | "scores:<density>:<page>"."""
    if not view or view in ("black", "null"):
        return "Blank"
    if view == "welcome":
        return "Welcome"
    if view == "counter":
        return "Counter"
    if view == "announcement":
        return "Announcement"
    if view == "schedule":
        return "Schedule"
    if view.startswith("scores:"):
        parts = view.split(":")
        density = "totals" if len(parts) > 1 and parts[1] == "totals" else "detailed"
        page = parts[2] if len(parts) > 2 else "all"
        page_label = "all (rotating)" if page in ("", "all") else "page " + page
        return f"Standings — {density}, {page_label}"
    return view


def _mode_breakdowns(modes, screens):
    """Decorate each saved mode with the per-screen views it would apply, so the
    admin can show what clicking a mode does.

    `views` is a JSON list of view strings in screen order. Applying a mode pairs
    them with the screens via zip(), so a mode saved with fewer views than there
    are screens leaves the surplus screens untouched (shown here as "unchanged"),
    and surplus views (more views than screens) are dropped. is_active mirrors
    that: it matches over the covered screens only — the shorter of the two —
    so a mode reads as active exactly when re-clicking it would be a no-op.
    (Initial paint only; JS keeps the highlight current after live edits.)"""
    current = [str(s.view) or "black" for s in screens]
    # Positional label (/1, /2…) plus the operator's name when the screen was renamed.
    labels = [f"/{i + 1}" + (f" — {s.friendly_name}" if s.friendly_name else "")
              for i, s in enumerate(screens)]
    out = []
    for mode in modes:
        views = mode.views if isinstance(mode.views, list) else []
        normalised = [v or "black" for v in views]
        rows = []
        for i in range(len(current)):
            if i < len(normalised):
                rows.append({"label": labels[i],
                             "pretty": _pretty_view(normalised[i]), "unchanged": False})
            else:
                rows.append({"label": labels[i],
                             "pretty": "unchanged", "unchanged": True})
        covered = min(len(current), len(normalised))
        out.append({
            "id": mode.id,
            "name": mode.name,
            "rows": rows,
            "views_json": json.dumps(normalised, separators=(',', ':')),
            "is_active": covered > 0 and current[:covered] == normalised[:covered],
        })
    return out


def publisher_overview_rows(tenant, variables):
    """Per-round summary for the Publisher overview page.

    One dict per round 1..nb_rounds with the counts shown in the table and the
    underlying per-table id lists, so the page can keep the aggregates accurate
    as it applies the same scorer.* / publish.state WebSocket deltas the Scoring
    page consumes (rather than re-querying on every keystroke). The sheet state
    mirrors the Scoring page badge exactly: a validated sheet is never also
    counted as in-progress (filled_keys already excludes validated tables).
    """
    nb_rounds = variables.nb_rounds or 0

    total_per = defaultdict(int)
    mp_per = defaultdict(int)
    for rn, tn, mp in Seat.objects.filter(tenant=tenant).values_list('round_nb', 'table_nb', 'minipoints'):
        total_per[(rn, tn)] += 1
        if mp is not None:
            mp_per[(rn, tn)] += 1

    tables_per_round = defaultdict(int)
    scored_tables = defaultdict(list)  # round -> [table_nb] (every seat has minipoints)
    for (rn, tn), total in total_per.items():
        tables_per_round[rn] += 1
        if total > 0 and mp_per[(rn, tn)] == total:
            scored_tables[rn].append(tn)

    validated_keys = set(
        ScoreSheet.objects.filter(tenant=tenant, validated=True).values_list('round_nb', 'table_nb')
    )
    filled_keys = set(
        Hand.objects.filter(tenant=tenant, win_by__isnull=False)
            .values_list('round_nb', 'table_nb').distinct()
    ) - validated_keys

    validated_tables = defaultdict(list)
    for rn, tn in validated_keys:
        validated_tables[rn].append(tn)
    inprogress_tables = defaultdict(list)
    for rn, tn in filled_keys:
        inprogress_tables[rn].append(tn)

    published = set(
        PublishedRound.objects.filter(tenant=tenant).values_list('round_nb', flat=True)
    )

    rows = []
    for r in range(1, nb_rounds + 1):
        total = tables_per_round.get(r, 0)
        scored = sorted(scored_tables.get(r, []))
        rows.append({
            'round_nb': r,
            'tables_total': total,
            'scored_tables': scored,
            'inprogress_tables': sorted(inprogress_tables.get(r, [])),
            'validated_tables': sorted(validated_tables.get(r, [])),
            'published': r in published,
            'complete': total > 0 and len(scored) == total,
        })
    return rows


@user_passes_test(lambda u: u.is_staff)
def admin_print_EMA(request):
    variables = get_variables(request)
    if variables.rules == "MCR":
        wb = load_workbook(filename=str(BASE_DIR / "files/report_template.xlsx"), data_only=True)
        sheet_ranges = wb['MCR template']
    else:
        wb = load_workbook(filename=str(BASE_DIR / "files/report_template_Riichi.xlsx"), data_only=True)
        sheet_ranges = wb['Riichi template']
    scores_json = scores_per_player_json(request, True)
    for row, player in enumerate(scores_json):
        items = [
            variables.fullname,
            len(scores_json),
            player["pos"],
            player["first_name"],
            player["last_name"],
            player["EMA_ID"],
            player["total"]["tp"],
            player["total"]["mp"],
            "YES" if player["EMA_ID"] != "" else "NO",
            player["flag"].upper(),
            datetime.today().strftime('%d/%m/%Y'),
            variables.countrycourt,   # Countrycourt: organising federation code (settings)
            variables.city,
            2,
            variables.title,
            variables.rules,   # discipline column: "MCR" or "Riichi", not hardcoded
            variables.period,
            2,
            "NO",
        ]
        for col, item in enumerate(items):
            sheet_ranges.cell(row=row + 2, column=col + 1).value = item
    buf = io.BytesIO()
    wb.save(buf)
    response = HttpResponse(
        buf.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = 'attachment; filename="EMA_report.xlsx"'
    return response


@user_passes_test(lambda u: u.is_staff)
def admin_upload_from_template(request):
    tenant = get_tenant(request)
    if request.method == 'POST':
        try:
            attached_file = request.FILES.get("myfile", None)
            if attached_file is None:
                return options(request)
            tmp_dir = BASE_DIR / "tmp"
            tmp_file = tmp_dir / "template.xlsx"
            if tmp_file.is_file():
                os.remove(str(tmp_file))
            fs = FileSystemStorage(location=str(tmp_dir))
            fs.save("template.xlsx", attached_file)

            Player.objects.filter(tenant=tenant).delete()

            wb = load_workbook(filename=str(tmp_file), data_only=True, read_only=True)

            Schedule.objects.filter(tenant=tenant).delete()
            sched_sheet = wb['Schedule']
            schedule_rows = list(sched_sheet.iter_rows(min_row=2, max_col=4, values_only=True))
            # Column 4 ("Is round") is written by our own export; the shipped template
            # has no such column (cell is None), so fall back to guessing from the name
            # (a row named "Round N" / "Session N" is a playing round). Either way staff
            # can correct any misclassification afterwards in Tournament settings.
            Schedule.objects.bulk_create([
                Schedule(tenant=tenant, day=row[0], time=row[1], name=row[2],
                         is_round=bool(row[3]) if row[3] is not None else _name_is_round(row[2]))
                for row in schedule_rows if row[0] is not None
            ])

            opt_sheet = wb['Options']
            opt_vals = [row[1] for row in opt_sheet.iter_rows(min_row=1, max_row=6, max_col=2, values_only=True)]
            variables = get_variables(request)
            variables.fullname = opt_vals[0] or ""
            variables.title = opt_vals[1] or ""
            variables.nb_rounds = opt_vals[2] or 0
            variables.city = opt_vals[3] or ""
            variables.period = opt_vals[4] or ""
            variables.rules = opt_vals[5] or "MCR"
            variables.save()

            # Roster: one Player per real person. The optional 'rand' column is a
            # pre-assigned draw number; when present the person is linked to their
            # seats below, otherwise the draw is made later (randomize / team draw).
            players_sheet = wb['Players']
            # Column 7 ("Email") is written by our own export; the shipped template
            # stops at column 6, so the extra cell simply comes back as None there.
            player_rows = list(players_sheet.iter_rows(min_row=2, max_col=7, values_only=True))
            player_objs = []
            any_team = False
            all_have_team = True
            for row in player_rows:
                if row[0] is None:
                    break
                last_name_raw, first_name_raw, ema_raw, country, team_raw, rand_raw, email_raw = row
                # Make first_name, last_name into title-case strings:
                first_name_raw = first_name_raw.strip().title() if isinstance(first_name_raw, str) else ""
                last_name_raw = last_name_raw.strip().title() if isinstance(last_name_raw, str) else ""
                full_name = f"{first_name_raw} {last_name_raw}"
                team = (team_raw or "").strip()
                if team:
                    any_team = True
                else:
                    all_have_team = False
                try:
                    ema = f"{ema_raw:08d}"
                except Exception:
                    ema = ""
                # The optional 'rand' column is the competitor's draw number. The
                # draw lives on the Player; seats are keyed by it. None until drawn.
                try:
                    draw_number = int(rand_raw) if rand_raw is not None else None
                except (TypeError, ValueError):
                    draw_number = None
                email = (email_raw or "").strip() if isinstance(email_raw, str) else ""
                player_objs.append(Player(
                    tenant=tenant, full_name=full_name, EMA_ID=ema,
                    country=country or "", email=email, team=team, draw_number=draw_number,
                ))
            if any_team and not all_have_team:
                raise ValueError("All players must have a team when teams are used, but some team cells are empty.")
            Player.objects.bulk_create(player_objs)

            # The roster is all-or-nothing on teams (enforced just above), so the
            # presence of any team name is the single signal for has_teams, which
            # gates team standings/columns/printouts everywhere.
            variables.has_teams = any_team
            variables.save()

            # Disambiguate first names for display (two "Chris" -> "Chris."/"Christo").
            # bulk_create skips Player.save(), so set first_name here in one bulk_update.
            first_names = [unidecode(p.full_name) for p in player_objs]
            for player in player_objs:
                value = player.full_name.split(" ")[0]
                firstname = value
                for _ in range(10):
                    ud_value = unidecode(value)
                    if sum(fn[:len(ud_value)] == ud_value for fn in first_names) > 1:
                        value += player.full_name[len(value):len(value) + 1]
                    else:
                        break
                value = value.rstrip()
                player.first_name = value + "." if value != firstname else value
            Player.objects.bulk_update(player_objs, ['first_name'])

            Hand.objects.filter(tenant=tenant).delete()
            ScoreSheet.objects.filter(tenant=tenant).delete()
            Seat.objects.filter(tenant=tenant).delete()
            # The new schedule starts with empty scores, so any rounds that were
            # published for the previous tournament are now stale — unpublish them all.
            PublishedRound.objects.filter(tenant=tenant).delete()
            nb_players = len(player_objs)
            nb_tables = nb_players // 4
            pos_sheet = wb['{0} players'.format(nb_players)]
            # Materialize the full seating sheet once (rows 3..3+nb_rounds-1,
            # cols 2..2+5*nb_tables-1). Each cell holds the draw number seated there.
            pos_rows = list(pos_sheet.iter_rows(
                min_row=3, max_row=2 + variables.nb_rounds,
                min_col=2, max_col=1 + 5 * nb_tables,
                values_only=True,
            ))
            seats_to_create = []
            for round_idx, row in enumerate(pos_rows):
                for table_nb in range(nb_tables):
                    for wind in range(4):
                        cell = row[wind + 5 * table_nb]
                        draw_number = int(cell) if cell is not None else 0
                        seats_to_create.append(Seat(
                            tenant=tenant,
                            draw_number=draw_number,
                            round_nb=round_idx + 1,
                            table_nb=table_nb + 1,
                            wind=wind + 1,
                            minipoints=None,
                            tablepoints=None,
                        ))
            Seat.objects.bulk_create(seats_to_create, batch_size=500)
            wb.close()
            from ..signals import invalidate_leaderboard
            invalidate_leaderboard(tenant.subdomain)
            broadcast_publish_state(tenant.subdomain, {'published_rounds': []})
        except Exception:
            Player.objects.filter(tenant=tenant).delete()
            Hand.objects.filter(tenant=tenant).delete()
            ScoreSheet.objects.filter(tenant=tenant).delete()
            Seat.objects.filter(tenant=tenant).delete()
            PublishedRound.objects.filter(tenant=tenant).delete()
            Schedule.objects.filter(tenant=tenant).delete()
            variables = get_variables(request)
            variables.nb_rounds = 0
            variables.save()
            error_traceback = traceback.format_exc()
            return options(
                request,
                error="Error while creating tournament: <br/><code>{0}</code>".format(error_traceback),
            )

        return options(request)


@user_passes_test(lambda u: u.is_staff)
def admin_export_to_template(request):
    """Export the current tournament as an Excel file in the import-template format.

    The workbook is built from scratch (not from MahjongTemplate.xlsx) with exactly
    the sheets admin_upload_from_template reads back: Options, Players, Schedule and
    a single "<N> players" seating sheet for this tournament's field size. Every
    field the importer restores is written, so staff can round-trip a tournament
    (back it up, edit offline, re-upload) without losing data. Scores are not
    included — re-importing intentionally clears them.
    """
    tenant = get_tenant(request)
    variables = get_variables(request)

    wb = Workbook()

    # Options: label in col A (for humans), value in col B (what the importer reads).
    opt_sheet = wb.active
    opt_sheet.title = 'Options'
    for row, (label, value) in enumerate([
        ('Competition name', variables.fullname),
        ('Short name (initials)', variables.title),
        ('Number of rounds', variables.nb_rounds),
        ('City', variables.city),
        ('Period', variables.period),
        ('Rules', variables.rules),
    ], start=1):
        opt_sheet.cell(row=row, column=1, value=label)
        opt_sheet.cell(row=row, column=2, value=value)

    # Players: id order is the original roster row order (matches the draw exports).
    # Columns 1-6 mirror the shipped template; "Email" is appended so it round-trips.
    roster = list(Player.objects.filter(tenant=tenant).order_by('id'))
    players_sheet = wb.create_sheet('Players')
    players_sheet.append([
        'Last name', 'First name', 'EMA number', 'Country',
        'Team name (optional)', 'Random position (1 - # of players)', 'Email (optional)',
    ])
    for player in roster:
        first_name = player.full_name.split(" ")[0]
        last_name = " ".join(player.full_name.split(" ")[1:])
        players_sheet.append([
            last_name,
            first_name,
            # EMA_ID is stored zero-padded ("00001234"); export the number so the
            # importer's f"{ema:08d}" reproduces it (a blank stays blank).
            int(player.EMA_ID) if player.EMA_ID else None,
            player.country,
            player.team or None,
            player.draw_number,
            player.email or None,
        ])

    # Schedule: Date / Time / Name; the "Is round" flag is appended so it round-trips
    # (the importer otherwise re-guesses it from the name).
    sched_sheet = wb.create_sheet('Schedule')
    sched_sheet.append(['Date', 'Time', 'Name', 'Is round'])
    for item in Schedule.objects.filter(tenant=tenant).order_by('id'):
        sched_sheet.append([item.day, item.time, item.name, item.is_round])

    # Seating: a single "<N> players" sheet for this field size, built from the real
    # seating. Draw numbers are laid out as round rows x table blocks of E/S/W/N,
    # with one spacer column between tables (col = 2 + wind + 5*table, both 0-based) —
    # exactly the layout admin_upload_from_template reads back.
    nb_players = len(roster)
    nb_tables = nb_players // 4
    seats = list(Seat.objects.filter(tenant=tenant))
    if seats and nb_tables:
        pos_sheet = wb.create_sheet('{0} players'.format(nb_players))
        for table_nb in range(1, nb_tables + 1):
            base = 2 + 5 * (table_nb - 1)
            pos_sheet.cell(row=1, column=base, value='Table {0}'.format(table_nb))
            for wind, label in enumerate(['East', 'South', 'West', 'North']):
                pos_sheet.cell(row=2, column=base + wind, value=label)
        nb_rounds = max(variables.nb_rounds, max(s.round_nb for s in seats))
        for round_nb in range(1, nb_rounds + 1):
            pos_sheet.cell(row=round_nb + 2, column=1, value='R{0}'.format(round_nb))
        for seat in seats:
            col = 2 + (seat.wind - 1) + 5 * (seat.table_nb - 1)
            pos_sheet.cell(row=seat.round_nb + 2, column=col, value=seat.draw_number)

    buf = io.BytesIO()
    wb.save(buf)
    # Keep the download name to a safe charset (the title is free text).
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (variables.title or ""))
    filename = safe.strip("_") or "tournament"
    response = HttpResponse(
        buf.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = 'attachment; filename="{0}.xlsx"'.format(filename)
    return response


@user_passes_test(lambda u: u.is_staff)
def admin_team_draw(request):
    tenant = get_tenant(request)
    roster = list(Player.objects.filter(tenant=tenant).order_by('full_name'))

    # id order is the original row order of the Excel "Players" sheet. The CSV
    # export sorts on this so the drawn numbers line up with the template rows.
    order = {p.id: i for i, p in enumerate(sorted(roster, key=lambda p: p.id), start=1)}

    teams_dict = {}
    for p in roster:
        if p.team:
            teams_dict.setdefault(p.team, []).append({
                "id": p.id,
                "original_index": order[p.id],
                "full_name": p.full_name,
                "first_name": p.first_name,
                "country": p.country,
                "flag": _country_flag(p.country),
                "EMA_ID": p.EMA_ID,
            })

    teams_list = [
        {"name": name, "players": players}
        for name, players in sorted(teams_dict.items())
    ]

    nb_teams = len(teams_list)

    # Existing draw, if any: the competitors who already have a draw number,
    # grouped by team.
    drawn = [p for p in roster if p.draw_number is not None and p.team]

    saved_draw = []
    if drawn:
        draw_teams = {}
        for p in drawn:
            draw_teams.setdefault(p.team, []).append({
                "full_name": p.full_name,
                "rand_id": p.draw_number,
                "original_index": order.get(p.id, 0),
            })
        slot = 1
        for team_name in sorted(draw_teams.keys()):
            members = sorted(draw_teams[team_name], key=lambda x: x["rand_id"])
            saved_draw.append({
                "slot": slot,
                "team_name": team_name,
                "players": members,
            })
            slot += 1

    template = loader.get_template('mahj/admin_team_draw.html')
    context = {
        "teams_json": json.dumps(teams_list),
        "nb_teams": nb_teams,
        "saved_draw_json": json.dumps(saved_draw) if saved_draw else "null",
    }
    return HttpResponse(template.render(context, request))


@user_passes_test(lambda u: u.is_staff)
def admin_team_draw_save(request):
    if request.method != 'POST':
        return HttpResponse('POST required', status=405)

    tenant = get_tenant(request)
    data = json.loads(request.body)
    assignments = data.get('assignments', [])  # [{player_id, rand_id}] rand_id = draw number

    player_ids = [a['player_id'] for a in assignments]
    draw_numbers = [a['rand_id'] for a in assignments]  # rand_id = the drawn number
    players = {p.id: p for p in Player.objects.filter(tenant=tenant, id__in=player_ids)}

    # Free the target draw numbers from any current holder and clear these
    # competitors' current numbers, then assign each their drawn number. Clearing
    # first keeps the per-tenant unique draw_number constraint satisfied.
    Player.objects.filter(tenant=tenant, draw_number__in=draw_numbers).update(draw_number=None)
    Player.objects.filter(tenant=tenant, id__in=player_ids).update(draw_number=None)
    to_update = []
    for a in assignments:
        player = players.get(a['player_id'])
        if player is not None:
            player.draw_number = a['rand_id']
            to_update.append(player)
    Player.objects.bulk_update(to_update, ['draw_number'])

    return HttpResponse('OK')


@user_passes_test(lambda u: u.is_staff)
def admin_player_draw(request):
    """Live individual draw: competitors arrive one at a time, physically draw a
    number, and the operator types it in. Each assignment is saved immediately
    (via admin_player_draw_assign) so the seating chart is live as the desk runs.
    The draw is recorded as Player.draw_number, same as Randomize / Team draw."""
    tenant = get_tenant(request)
    roster = list(Player.objects.filter(tenant=tenant).order_by('full_name'))

    # id order is the original row order of the Excel "Players" sheet, so a CSV
    # export lines up with the template rows (matches the team-draw export).
    order = {p.id: i for i, p in enumerate(sorted(roster, key=lambda p: p.id), start=1)}

    players = [{
        "id": p.id,
        "original_index": order[p.id],
        "full_name": p.full_name,
        "first_name": p.first_name,
        "country": p.country,
        "flag": _country_flag(p.country),
        "draw_number": p.draw_number,
    } for p in roster]

    draw_numbers = sorted({s.draw_number for s in Seat.objects.filter(tenant=tenant)})

    template = loader.get_template('mahj/admin_player_draw.html')
    context = {
        "players_json": json.dumps(players),
        "draw_numbers_json": json.dumps(draw_numbers),
    }
    return HttpResponse(template.render(context, request))


@user_passes_test(lambda u: u.is_staff)
def admin_player_draw_assign(request):
    """Assign (or clear) one competitor's draw number for the live individual draw.

    POST {player_id, draw_number} where draw_number is the number the competitor
    physically drew, or null to clear it (undo). The availability check is done
    here under a row lock so two registration desks can't hand out the same
    number: the request fails if the number isn't a real draw slot or is already
    held by someone else."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'}, status=405)

    tenant = get_tenant(request)
    data = json.loads(request.body)
    player_id = data.get('player_id')
    draw_number = data.get('draw_number')  # int to assign, None to clear

    with transaction.atomic():
        try:
            player = Player.objects.select_for_update().get(tenant=tenant, id=player_id)
        except Player.DoesNotExist:
            return JsonResponse({'ok': False, 'error': 'Unknown competitor'}, status=404)

        if draw_number is None:
            player.draw_number = None
            player.save(update_fields=['draw_number'])
            return JsonResponse({'ok': True, 'player_id': player.id, 'draw_number': None})

        valid = set(Seat.objects.filter(tenant=tenant).values_list('draw_number', flat=True))
        if draw_number not in valid:
            return JsonResponse(
                {'ok': False, 'error': f'#{draw_number} is not a valid draw number'}, status=400)

        holder = (Player.objects
                  .select_for_update()
                  .filter(tenant=tenant, draw_number=draw_number)
                  .exclude(id=player.id)
                  .first())
        if holder is not None:
            return JsonResponse(
                {'ok': False,
                 'error': f'#{draw_number} is already taken by {holder.full_name}',
                 'holder': holder.full_name},
                status=409)

        player.draw_number = draw_number
        player.save(update_fields=['draw_number'])
        return JsonResponse({'ok': True, 'player_id': player.id, 'draw_number': draw_number})


# The metadata fields the Player editor may change. draw_number is deliberately
# NOT here: it's the draw itself (set by Randomize / Team draw / import) and the
# seating chart is keyed by it, so reassigning it belongs to those tools, not to
# a free-text metadata edit.
_PLAYER_EDITABLE_FIELDS = ['full_name', 'first_name', 'EMA_ID', 'country', 'email', 'team']


@user_passes_test(lambda u: u.is_staff)
def player_editor_save(request):
    """Persist inline edits from the Player editor table. Accepts JSON
    ``{"players": [{"id", <field>...}]}`` and bulk-updates the editable
    metadata; unknown ids are ignored, over-long values are rejected up front."""
    if request.method != 'POST':
        return HttpResponse('POST required', status=405)

    tenant = get_tenant(request)
    rows = json.loads(request.body).get('players', [])
    by_id = {p.id: p for p in Player.objects.filter(
        tenant=tenant, id__in=[r.get('id') for r in rows])}

    to_update = []
    for r in rows:
        player = by_id.get(r.get('id'))
        if player is None:
            continue
        for field in _PLAYER_EDITABLE_FIELDS:
            if field not in r:
                continue
            value = (r.get(field) or '').strip()
            max_length = Player._meta.get_field(field).max_length
            if max_length and len(value) > max_length:
                return HttpResponse(
                    f"{field} is too long: {len(value)} characters "
                    f"(maximum {max_length}).", status=400)
            setattr(player, field, value)
        # Mirror Player.save(): a blank first name falls back to the first token
        # of the full name (bulk_update bypasses the model's save()).
        if player.first_name == "":
            player.first_name = player.full_name.split(" ")[0]
        to_update.append(player)

    if to_update:
        Player.objects.bulk_update(to_update, _PLAYER_EDITABLE_FIELDS)
    return HttpResponse('OK')


# Public (display screens are public, like /scan and counter_start): serve the
# tenant's uploaded logo. Templates fall back to the static mcr_logo when unset,
# so this is only hit when a logo exists.
def logo(request):
    variables = get_variables(request)
    if not variables.logo:
        raise Http404
    resp = HttpResponse(bytes(variables.logo), content_type="image/png")
    resp["Cache-Control"] = "public, max-age=86400"
    resp["ETag"] = f'"{variables.logo_etag}"'
    return resp


@user_passes_test(lambda u: u.is_staff)
def update_logo(request):
    variables = get_variables(request)
    if request.POST.get("reset") == "1":
        variables.logo = None
        variables.logo_etag = ""
    else:
        f = request.FILES.get("logo")
        if f is None:
            return HttpResponseBadRequest("No file uploaded")
        if f.size > 2 * 1024 * 1024:
            return HttpResponseBadRequest("Logo too large (max 2 MB)")
        data = f.read()
        # Trust the bytes, not the extension: must be a real PNG.
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            return HttpResponseBadRequest("PNG files only")
        variables.logo = data
        variables.logo_etag = hashlib.md5(data).hexdigest()
    # Scope the write to the logo fields: a full-row save would also persist this
    # (possibly stale) instance's `counter`, which could stop a running round
    # timer. signals.py still invalidates the cached settings on post_save.
    variables.save(update_fields=['logo', 'logo_etag'])
    return HttpResponse("OK")


@user_passes_test(lambda u: u.is_staff)
def admin_reset(request):
    """Factory-reset the tournament: wipe every tenant row and its settings.

    Staff-only, POST-only, and gated behind a type-to-confirm + confirm dialog in
    the UI — this is irreversible. It clears all tournament data (roster, seating,
    hands, score sheets, published rounds, schedule) *and* the tenant's
    configuration (title/branding/format, logo, screens, screen modes, publish
    target, ceremony state), leaving a blank instance ready for a fresh import.
    Deleting the TournamentSettings row lets get_variables recreate it at defaults;
    its post_delete signal busts the settings cache and refreshes public displays.
    """
    if request.method != 'POST':
        return HttpResponseBadRequest("POST required")
    tenant = get_tenant(request)
    if tenant is None:
        return HttpResponseBadRequest("No tenant")
    from ..models import PublishTarget, TournamentSettings
    with transaction.atomic():
        Hand.objects.filter(tenant=tenant).delete()
        ScoreSheet.objects.filter(tenant=tenant).delete()
        Seat.objects.filter(tenant=tenant).delete()
        PublishedRound.objects.filter(tenant=tenant).delete()
        Player.objects.filter(tenant=tenant).delete()
        Schedule.objects.filter(tenant=tenant).delete()
        Screen.objects.filter(tenant=tenant).delete()
        ScreenMode.objects.filter(tenant=tenant).delete()
        CeremonyState.objects.filter(tenant=tenant).delete()
        PublishTarget.objects.filter(tenant=tenant).delete()
        # Deleting the settings row resets identity/branding/format, logo and the
        # round timer to defaults; get_variables recreates a fresh one on next read.
        TournamentSettings.objects.filter(tenant=tenant).delete()
    # Wake public displays and scorer pages: nothing is published anymore, the
    # screen set is gone, and the leaderboard/settings caches are stale.
    invalidate_leaderboard(tenant.subdomain)
    broadcast_publish_state(tenant.subdomain, {'published_rounds': []})
    broadcast_display(tenant.subdomain, 'screen.update', {'event': 'screens_changed'})
    return JsonResponse({'status': 'ok'})


@user_passes_test(lambda u: u.is_staff)
def publish_web(request):
    """Manually regenerate + upload the public static site (the "Publish to web"
    button). Publish also happens automatically on each round publish; this is the
    on-demand re-push. Runs in the background so the request returns at once."""
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    from ..publish.sftp_upload import is_configured
    if not is_configured(subdomain):
        return JsonResponse(
            {'status': 'error',
             'error': 'Static publish is not configured for this tenant '
                      '(add a publish target first).'},
            status=400)
    from ..publish.trigger import fire_static_export
    fire_static_export(subdomain)
    return JsonResponse({'status': 'ok', 'message': 'Publishing to the web…'})


@user_passes_test(can_access_admin)
def publish_status(request):
    """Poll the running (or last) publish job — drives the shell progress toast.
    Any admin role can read it (auto-publish fires on a publisher's round publish,
    not just staff's manual push). Returns {phase, pct, message, error}; phase is
    idle when nothing is running."""
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    from ..publish.trigger import get_progress
    return JsonResponse(get_progress(subdomain) or {'phase': 'idle'})


@user_passes_test(lambda u: u.is_staff)
def publish_target_save(request):
    """Save this tenant's SFTP publish target (staff only).

    Secrets are write-only: a blank password/key leaves the stored value
    untouched (so the form never has to echo it back), and clear_password /
    clear_key wipe it. Stored encrypted via publish.secrets."""
    if request.method != 'POST':
        return HttpResponseForbidden('POST required')
    tenant = get_tenant(request)
    if tenant is None:
        return JsonResponse({'status': 'error', 'error': 'No tenant.'}, status=400)
    from ..models import PublishTarget
    from ..publish import secrets as publish_secrets
    target, _ = PublishTarget.objects.get_or_create(tenant=tenant)

    def _flag(name):
        return request.POST.get(name, '').strip().lower() in ('true', '1', 'on', 'yes')

    target.enabled = _flag('enabled')
    target.host = request.POST.get('host', '').strip()
    target.username = request.POST.get('username', '').strip()
    target.path = request.POST.get('path', '').strip()
    target.host_key = request.POST.get('host_key', '').strip()
    port_raw = request.POST.get('port', '').strip() or '22'
    try:
        target.port = int(port_raw)
    except ValueError:
        return JsonResponse(
            {'status': 'error', 'error': f'Port must be a number (got {port_raw!r}).'},
            status=400)

    password = request.POST.get('password', '')
    if request.POST.get('clear_password') == '1':
        target.password_enc = None
    elif password:
        target.password_enc = publish_secrets.encrypt(password)

    key = request.POST.get('private_key', '')
    if request.POST.get('clear_key') == '1':
        target.private_key_enc = None
    elif key.strip():
        target.private_key_enc = publish_secrets.encrypt(key)

    target.save()

    # The advertised spectator URL is a TournamentSettings field (cached and read
    # on every public request), but edited on this page next to the SFTP target.
    variables = get_variables(request)
    public_url = request.POST.get('public_url', '').strip()
    if public_url != variables.public_url:
        variables.public_url = public_url
        variables.save(update_fields=['public_url'])  # signals bust the cache
    return JsonResponse({'status': 'ok'})


@user_passes_test(lambda u: u.is_staff)
def publish_target_test(request):
    """Open + close an SFTP connection using the values currently in the form —
    not the saved target — so staff can verify before saving. A blank password/
    key field falls back to the stored secret, so you can test an unchanged
    credential without re-typing it."""
    if request.method != 'POST':
        return HttpResponseForbidden('POST required')
    tenant = get_tenant(request)
    from ..models import PublishTarget
    from ..publish import secrets as publish_secrets
    from ..publish.sftp_upload import PublishConfig, _connect

    host = request.POST.get('host', '').strip()
    if not host:
        return JsonResponse({'status': 'error', 'error': 'Enter a host first.'}, status=400)
    port_raw = request.POST.get('port', '').strip() or '22'
    try:
        port = int(port_raw)
    except ValueError:
        return JsonResponse(
            {'status': 'error', 'error': f'Port must be a number (got {port_raw!r}).'},
            status=400)

    stored = (PublishTarget.objects.filter(tenant=tenant).order_by('id').first()
              if tenant else None)
    password = request.POST.get('password', '')
    if not password and stored and request.POST.get('clear_password') != '1':
        password = publish_secrets.decrypt(stored.password_enc)
    key = request.POST.get('private_key', '')
    if not key.strip() and stored and request.POST.get('clear_key') != '1':
        key = publish_secrets.decrypt(stored.private_key_enc)

    cfg = PublishConfig(
        host=host, port=port,
        username=request.POST.get('username', '').strip(),
        path=request.POST.get('path', '').strip() or '.',
        password=password,
        key_data=key,
        host_key=request.POST.get('host_key', '').strip(),
    )
    try:
        client = _connect(cfg)
        client.open_sftp().close()
        client.close()
    except Exception as e:
        return JsonResponse({'status': 'error', 'error': str(e)}, status=400)
    return JsonResponse({'status': 'ok', 'message': f'Connected to {cfg.host}.'})


# The round timer is server-authoritative. `counter` holds an absolute epoch-ms
# "gong moment": the instant the round starts (and the start gong sounds). Before
# it, screens render a synchronized lead window + 3-2-1 countdown; after it, the
# elapsed/remaining time. <= 0 means stopped / never started.
#
# Start = now + LEAD + COUNTDOWN. The LEAD is dead time during which every screen
# has received the broadcast but nothing visible happens yet, so a screen whose
# message is slightly delayed still catches the very first "3" — all rooms count
# down (and gong) in lockstep. Both constants are mirrored in display_counter.html
# and admin_display.html; keep them in sync.
COUNTER_LEAD_MS = 1000
COUNTER_COUNTDOWN_MS = 3000


def counter_start(request):
    """Read (no args, public — screens poll this) or command the round timer.

    Writes (?action=start|stop) are restricted to display operators and must be
    POSTs: only an explicit admin action may start/stop the counter. Clients never
    supply a timestamp — the server computes it — so projector screens are pure
    renderers and can't race each other or be reset by a stray request.
    """
    tenant = get_tenant(request)
    action = request.GET.get('action')
    if action in ('start', 'stop'):
        if request.method != 'POST' or not is_display_op(request.user):
            return HttpResponseForbidden('forbidden')
        if action == 'start':
            value = int(time.time() * 1000) + COUNTER_LEAD_MS + COUNTER_COUNTDOWN_MS
        else:  # stop / reset
            value = -1
        if tenant is not None:
            set_counter(tenant, value)
            broadcast_display(tenant.subdomain, 'counter.update', {
                'event': 'counter_update',
                'counter': value,
                'server_now': int(time.time() * 1000),
            })
    return JsonResponse({
        'counter': get_counter(tenant),
        'server_now': int(time.time() * 1000),
    })


def _apply_set_variable(request, variables):
    """Persist ``?variables-<field>=<value>`` params onto the tenant settings and
    return the response to send back. Shared by the Display page (screen-layout
    tuning) and the Tournament settings page (identity + round length)."""
    # Staff-only fields: display operators may tune the layout, but not the
    # round length (changing it mid-round would desync every screen's timer).
    staff_only_fields = {"total_time"}
    # The round timer is written only by counter_start (server-authoritative);
    # it must never be settable through this generic field loop, or a stray
    # `variables-counter=...` would stop/reset a running clock.
    protected_fields = {"counter"}
    touched_fields = []
    for var in request.GET.keys():
        if "variables-" in var:
            field = var.replace("variables-", "")
            if field in protected_fields:
                continue
            if field in staff_only_fields and not request.user.is_staff:
                continue
            if hasattr(variables, field):
                value = request.GET.get(var)
                # Coerce booleans: every GET value is a string, and a non-empty
                # string ("false") is truthy — so a raw setattr would store True
                # for both. Map the usual truthy spellings instead.
                if variables._meta.get_field(field).get_internal_type() == 'BooleanField':
                    value = value.strip().lower() in ('true', '1', 'on', 'yes')
                setattr(variables, field, value)
                touched_fields.append(field)
    if touched_fields:
        # Reject over-long text here, before it reaches the DB: on
        # PostgreSQL an oversized value raises a bare 500, which the admin
        # UI showed silently. Returning a readable 400 instead lets the
        # page surface exactly which field was too long, and why.
        for field in touched_fields:
            max_length = getattr(variables._meta.get_field(field), "max_length", None)
            value = getattr(variables, field)
            if max_length and isinstance(value, str) and len(value) > max_length:
                label = _VARIABLE_LABELS.get(field, field)
                return HttpResponse(
                    f"{label} is too long: {len(value)} characters "
                    f"(maximum {max_length}).",
                    status=400)
        # Scope the write to the fields actually edited: a full-row save would
        # also persist this instance's `counter`, which could stop a running
        # round timer. signals.py still busts the cache on post_save.
        try:
            variables.save(update_fields=touched_fields)
        except Exception as exc:
            # Any other save failure (e.g. a non-numeric value for a number
            # field) — return the reason rather than a silent 500.
            return HttpResponse(f"Could not save: {exc}", status=400)
    return HttpResponse(str(variables))


def _save_schedule(request, tenant):
    """Replace the tenant's schedule with the rows posted from the settings editor.

    The editor sends the whole agenda as JSON (ordered), so a full delete+recreate
    keeps row order (``Schedule`` is read ``order_by('id')`` everywhere) without
    tracking per-row ids. Returns the count of round-rows so the UI can flag a
    mismatch with ``nb_rounds`` (the Nth round-row maps to round N)."""
    try:
        rows = json.loads(request.POST.get('schedule', '[]'))
        if not isinstance(rows, list):
            raise ValueError("schedule must be a list")
    except (ValueError, json.JSONDecodeError) as exc:
        return HttpResponse(f"Could not read the schedule: {exc}", status=400)

    max_len = Schedule._meta.get_field('name').max_length
    objs = []
    for row in rows:
        day = (row.get('day') or "").strip()
        time = (row.get('time') or "").strip()
        name = (row.get('name') or "").strip()
        # A wholly blank line is dropped rather than stored as an empty agenda row.
        if not (day or time or name):
            continue
        for value, label in ((day, "Day"), (time, "Time"), (name, "Name")):
            if len(value) > max_len:
                return HttpResponse(
                    f"{label} is too long: {len(value)} characters "
                    f"(maximum {max_len}).", status=400)
        objs.append(Schedule(tenant=tenant, day=day, time=time, name=name,
                             is_round=bool(row.get('is_round'))))

    Schedule.objects.filter(tenant=tenant).delete()
    Schedule.objects.bulk_create(objs)

    # player_rounds (player modal) reads the schedule, and the projector Schedule
    # screen renders it — refresh both.
    invalidate_leaderboard(tenant.subdomain)
    broadcast_display(tenant.subdomain, 'screen.update', {'event': 'screen_update'})

    return JsonResponse({'rounds': sum(1 for o in objs if o.is_round)})


@user_passes_test(can_access_admin)
def options(request, error=None):
    tenant = get_tenant(request)
    if request.GET.get('logout') == "1":
        logout(request)
        return HttpResponseRedirect('admin')

    template = loader.get_template('mahj/admin.html')
    page = request.GET.get('page')
    
    if is_scorer(request.user) and not request.user.is_staff and page is None:
        page = "scoring"
    if is_display_op(request.user) and not request.user.is_staff and page is None:
        page = "display"
    # Publishers manage publishing from the scoring page.
    if is_publisher(request.user) and not request.user.is_staff and page is None:
        page = "scoring"

    if page == "welcome" or page is None:
        page = "welcome"
        from ..publish.sftp_upload import is_configured as _static_publish_configured
        from ..scoring import _last_complete_round, publish_state
        variables = get_variables(request)
        nb_players = Player.objects.filter(tenant=tenant).count()
        nb_drawn = Player.objects.filter(tenant=tenant, draw_number__isnull=False).count()
        nb_screens = Screen.objects.filter(tenant=tenant).count()
        # _last_complete_round returns nb_rounds when it finds no incomplete seat —
        # which also happens with no seats at all, a false "all complete". Guard on
        # the seating chart actually existing.
        has_seats = Seat.objects.filter(tenant=tenant).exists()
        complete_round = _last_complete_round(tenant, variables) if has_seats else 0
        last_published, _ = publish_state(tenant, variables)
        # Warn when a schedule exists but its playing rounds don't line up with
        # nb_rounds: the Nth round-row maps to round N (scoring.player_rounds), so a
        # mismatch leaves per-round times blank/misaligned. Only flag once a
        # schedule has been set up — a fresh, empty schedule isn't "wrong".
        schedule_total = Schedule.objects.filter(tenant=tenant).count()
        schedule_rounds = Schedule.objects.filter(tenant=tenant, is_round=True).count()
        template2 = loader.get_template('mahj/admin_welcome.html')
        page_content = template2.render(
            {
                "error": error,
                "static_publish_enabled": _static_publish_configured(tenant.subdomain if tenant else ''),
                "variables": variables,
                "nb_players": nb_players,
                # A player is "drawn in" once assigned a draw number; the roster is
                # ready to play when every player holds one.
                "draw_done": nb_players > 0 and nb_drawn == nb_players,
                "nb_drawn": nb_drawn,
                "nb_screens": nb_screens,
                "complete_round": complete_round,
                "last_published": last_published,
                "schedule_rounds": schedule_rounds,
                "schedule_round_mismatch": schedule_total > 0 and schedule_rounds != variables.nb_rounds,
                # Server-authoritative round timer: >0 and in the future means a
                # round is counting down / running (the dashboard shows it live).
                "counter": get_counter(tenant),
            },
            request,
        )
    elif page == "display":
        variables = get_variables(request)
        if request.GET.get('action') == "set_variable":
            return _apply_set_variable(request, variables)
        elif request.GET.get('action') == "add_screen":
            Screen(tenant=tenant, name="", view="black").save()
            # 'screens_changed' (not plain 'screen_update') so the overview grid
            # redraws for the new screen count; per-screen displays reload either way.
            broadcast_display(tenant.subdomain, 'screen.update', {'event': 'screens_changed'})
            return HttpResponseRedirect('admin?page=display#configure-screens')
        elif request.GET.get('action') == "remove_screen":
            Screen.objects.filter(tenant=tenant).last().delete()
            broadcast_display(tenant.subdomain, 'screen.update', {'event': 'screens_changed'})
            return HttpResponseRedirect('admin?page=display#configure-screens')
        elif request.GET.get('action') == "identify_screens":
            # Flash each screen's positional number (/1, /2, …) as a corner badge
            # for a few seconds so an operator can match physical projectors to
            # their URLs. Reuses the existing 'screen.update' channel with a
            # distinct event the display socket intercepts without reloading —
            # see mahj/static/js/display_socket.js.
            broadcast_display(tenant.subdomain, 'screen.update', {'event': 'screen_identify'})
            return HttpResponse("")
        elif request.GET.get('action') == "add_mode":
            mode_name = request.POST.get('mode_name')
            screens = Screen.objects.filter(tenant=tenant).order_by('id')
            views_list = [str(screen.view) for screen in screens]
            ScreenMode(tenant=tenant, name=mode_name, views=views_list).save()
            return HttpResponseRedirect('admin?page=display#configure-screens')
        elif request.GET.get('rm_mode'):
            mode = ScreenMode.objects.get(tenant=tenant, id=request.GET.get('rm_mode'))
            mode.delete()
            return HttpResponseRedirect('admin?page=display')
        elif request.GET.get('action') == "set_all_views":
            # Bulk "All screens" control: point every screen at one view in a
            # single write + one broadcast (rather than N per-screen posts).
            # Mirrors set_mode's shape so the admin page can sync each card's
            # selects/previews from the returned list.
            view = request.GET.get('view') or 'black'
            screens = Screen.objects.filter(tenant=tenant).order_by('id')
            applied = []
            for screen in screens:
                screen.view = view
                screen.save()
                applied.append({'id': screen.id, 'view': screen.view})
            broadcast_display(tenant.subdomain, 'screen.update', {'event': 'screen_update'})
            return JsonResponse({'screens': applied})
        elif request.GET.get('set_mode'):
            mode = ScreenMode.objects.get(tenant=tenant, id=request.GET.get('set_mode'))
            views_list = mode.views if isinstance(mode.views, list) else []
            screens = Screen.objects.filter(tenant=tenant).order_by('id')
            applied = []
            for view, screen in zip(views_list, screens):
                screen.view = view
                screen.save()
                applied.append({'id': screen.id, 'view': screen.view})
            broadcast_display(tenant.subdomain, 'screen.update', {'event': 'screen_update'})
            # The admin page applies modes via AJAX so it can refresh the
            # selects/previews in place; a direct (non-AJAX) hit redirects back.
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'screens': applied})
            return HttpResponseRedirect('admin?page=display')
        screens = Screen.objects.filter(tenant=tenant).order_by('id')
        modes = ScreenMode.objects.filter(tenant=tenant).order_by('id')
        # A running ceremony takes over every screen, so the display controls
        # below are inert until it ends. Flag it so the page can warn up front;
        # the banner then tracks live 'ceremony.update' broadcasts over the socket.
        ceremony_state = CeremonyState.objects.filter(tenant=tenant).first()
        context = {
            "screens": screens,
            "modes": _mode_breakdowns(modes, screens),
            "nb_players": Player.objects.filter(tenant=tenant).count(),
            # Distinct non-empty team names — drives the "Standings — teams" page
            # picker (individual pages + team pages).
            "nb_teams": Player.objects.filter(tenant=tenant).exclude(team="")
                .values_list('team', flat=True).distinct().count(),
            "variables": variables,
            "ceremony_active": bool(ceremony_state and ceremony_state.phase != 'idle'),
            # Base URL the screens are reachable at, taken from THIS request so the
            # preview iframes stay same-origin (X-Frame-Options: SAMEORIGIN). In the
            # cloud this resolves to https://<tenant>.<BASE_DOMAIN> (proxy headers); in
            # the standalone build to http://<host>:<port>. A hardcoded
            # <BASE_DOMAIN> URL would be cross-origin — and unreachable — on a laptop.
            "screen_base": request.build_absolute_uri('/').rstrip('/'),
        }
        # Standalone (LAN-served) build: list the addresses other devices can use
        # to open a screen — loopback (this machine) + the LAN IP. The public IP is
        # filled in client-side (external lookup) with a port-forward warning.
        if settings.STANDALONE:
            from .helpers import lan_ip
            port = request.get_port()
            bases = [("This machine", f"http://127.0.0.1:{port}")]
            ip = lan_ip()
            if ip:
                bases.append(("Same network (LAN)", f"http://{ip}:{port}"))
            context["standalone"] = True
            context["access_bases"] = bases
            context["access_port"] = port
        template2 = loader.get_template('mahj/admin_display.html')
        page_content = template2.render(context, request)
    elif page == "settings":
        # Staff-only: a scorer/display op reaching ?page=settings gets nothing.
        if not request.user.is_staff:
            page_content = "None"
        else:
            variables = get_variables(request)
            if request.GET.get('action') == "set_variable":
                return _apply_set_variable(request, variables)
            if request.GET.get('action') == "save_schedule":
                return _save_schedule(request, tenant)
            template2 = loader.get_template('mahj/admin_settings.html')
            schedule_rows = [
                {"day": s.day or "", "time": s.time or "",
                 "name": s.name or "", "is_round": s.is_round}
                for s in Schedule.objects.filter(tenant=tenant).order_by('id')
            ]
            page_content = template2.render({
                "variables": variables,
                "schedule_rows": schedule_rows,
            }, request)
    elif page == "player_editor":
        # Staff-only: a scorer/display op reaching ?page=player_editor gets nothing.
        if not request.user.is_staff:
            page_content = "None"
        else:
            players = Player.objects.filter(tenant=tenant).order_by(
                F('draw_number').asc(nulls_last=True), 'full_name')
            player_rows = [
                {'id': p.id, 'draw_number': p.draw_number, 'full_name': p.full_name,
                 'first_name': p.first_name, 'EMA_ID': p.EMA_ID, 'country': p.country,
                 'email': p.email, 'team': p.team}
                for p in players
            ]
            # The seating-chart slots a draw number may be assigned to (the editor
            # rejects anything else before it reaches admin_player_draw_assign).
            valid_draw_numbers = sorted(set(
                Seat.objects.filter(tenant=tenant).values_list('draw_number', flat=True)))
            template2 = loader.get_template('mahj/admin_player_editor.html')
            page_content = template2.render(
                {"player_rows": player_rows,
                 "valid_draw_numbers": valid_draw_numbers}, request)
    elif page == "publish_target":
        # Staff-only: holds SFTP credentials, so a scorer/display op gets nothing.
        if not request.user.is_staff:
            page_content = "None"
        else:
            from ..models import PublishTarget
            target = PublishTarget.objects.filter(tenant=tenant).order_by('id').first()
            template2 = loader.get_template('mahj/admin_publish_target.html')
            page_content = template2.render({
                "target": target,
                # Secrets are write-only — never render the value, just whether one
                # is set, so the form can show a "configured" hint.
                "has_password": bool(target and target.password_enc),
                "has_key": bool(target and target.private_key_enc),
                # The advertised spectator URL lives on TournamentSettings (cached,
                # non-secret) but is edited here, next to the SFTP target.
                "public_url": get_variables(request).public_url,
                "subdomain": tenant.subdomain if tenant else '',
            }, request)
    elif page == "import_template":
        template2 = loader.get_template('mahj/admin_import_template.html')
        page_content = template2.render({}, request)
    elif page == "scoring":
        variables = get_variables(request)
        scores_json = scores_per_table_json(request)
        all_players = Player.objects.filter(tenant=tenant).order_by('full_name')
        try:
            nb_rounds = len(scores_per_player_json(request, True)[0]["scores"])
        except Exception:
            nb_rounds = 6
        template2 = loader.get_template('mahj/admin_scores_per_table.html')
        published_rounds = list(
            PublishedRound.objects.filter(tenant=tenant)
                .order_by('round_nb').values_list('round_nb', flat=True)
        )
        validated_keys = {
            f"{rn}-{tn}"
            for rn, tn in ScoreSheet.objects.filter(tenant=tenant, validated=True)
                                       .values_list('round_nb', 'table_nb')
        }
        filled_keys = {
            f"{rn}-{tn}"
            for rn, tn in Hand.objects.filter(tenant=tenant, win_by__isnull=False)
                                       .values_list('round_nb', 'table_nb')
                                       .distinct()
        } - validated_keys
        context = {
            'scores_json': scores_json,
            "players": all_players,
            "variables": variables,
            "active_round": nb_rounds + 1,
            "published_rounds": published_rounds,
            "subdomain": tenant.subdomain if tenant else '',
            "validated_keys": validated_keys,
            "filled_keys": filled_keys,
            # Only staff and publishers may publish/unpublish — the endpoint is
            # gated the same way, so keep the toggle disabled for plain scorer
            # accounts to avoid a dead control.
            "can_publish": is_publisher(request.user),
        }
        page_content = template2.render(context, request)
    elif page == "ceremony":
        template2 = loader.get_template('mahj/admin_ceremony.html')
        page_content = template2.render({
            "variables": get_variables(request),
            "subdomain": tenant.subdomain if tenant else '',
            "screens": Screen.objects.filter(tenant=tenant).order_by('id'),
            # Same-origin base for the preview iframes (see the display page):
            # cloud → https://<tenant>.<BASE_DOMAIN>, standalone → http://<host>:<port>.
            "screen_base": request.build_absolute_uri('/').rstrip('/'),
        }, request)
    elif page == "publisher_overview":
        # Publisher-only: a plain scorer reaching this page (?page=…) gets nothing.
        # is_publisher includes staff.
        if not is_publisher(request.user):
            page_content = "None"
        else:
            variables = get_variables(request)
            template2 = loader.get_template('mahj/admin_publisher_overview.html')
            page_content = template2.render({
                "rows": publisher_overview_rows(tenant, variables),
                "variables": variables,
                "subdomain": tenant.subdomain if tenant else '',
            }, request)
    elif page == "users":
        # Staff-only: a scorer/display op reaching ?page=users gets nothing.
        if not request.user.is_staff:
            page_content = "None"
        elif not reauth_ok(request):
            # Borrowed/unattended session: make staff re-enter their password
            # before exposing (or letting them touch) user management. Link-only
            # staff have no password to confirm, so they're shut out entirely.
            template2 = loader.get_template('mahj/admin_users_reauth.html')
            page_content = template2.render(
                {"link_only": not request.user.has_usable_password()}, request)
        else:
            user_rows = []
            for u in User.objects.all().order_by('username').prefetch_related('groups'):
                gnames = {g.name for g in u.groups.all()}
                user_rows.append({
                    "id": u.id,
                    "username": u.username,
                    "is_staff": u.is_staff,
                    "is_self": u.id == request.user.id,
                    "last_login": u.last_login,
                    "has_password": u.has_usable_password(),
                    "roles": [{"name": n, "active": n in gnames} for n in ROLE_GROUPS],
                })
            template2 = loader.get_template('mahj/admin_users.html')
            page_content = template2.render({
                "user_rows": user_rows,
                "role_groups": ROLE_GROUPS,
                "link_validity_days": settings.SESAME_MAX_AGE // 86400,
            }, request)
    elif page == "database_restore":
        # Staff-only, and — like user management — re-confirm the password first:
        # this page can WIPE the live DB, so a borrowed session must re-auth.
        if not request.user.is_staff:
            page_content = "None"
        elif not reauth_ok(request):
            template2 = loader.get_template('mahj/admin_users_reauth.html')
            page_content = template2.render(
                {"link_only": not request.user.has_usable_password(),
                 "reauth_next": "database_restore"}, request)
        else:
            if settings.STANDALONE:
                from .. import standalone_backup
                restore_ctx = {
                    "groups": standalone_backup.list_snapshot_groups(),
                    "db_name": standalone_backup.CONFIRM_TOKEN,
                    "pull_configured": False,   # off-host pull is Postgres-only
                    "standalone": True,         # restore applies on relaunch
                }
            else:
                restore_ctx = {
                    "groups": list_backups(),
                    "db_name": settings.DATABASES['default']['NAME'],
                    "pull_configured": bool(os.environ.get('REMOTE')),
                }
            template2 = loader.get_template('mahj/admin_database_restore.html')
            page_content = template2.render(restore_ctx, request)
    else:
        page_content = "None"

    from ..publish.sftp_upload import is_configured as _static_publish_configured
    context = {
        "username": request.user.username,
        "page": page,
        "page_content": page_content,
        "user_is_scorer": is_scorer(request.user),
        "user_is_display_op": is_display_op(request.user),
        "user_is_publisher": is_publisher(request.user),
        "uses_teams": get_variables(request).has_teams,
        # Drives the shell-wide publish progress toast (polls publish_status) — only
        # when web publishing is configured, so idle installs don't poll.
        "static_publish_enabled": _static_publish_configured(tenant.subdomain if tenant else ''),
    }
    return HttpResponse(template.render(context, request))
