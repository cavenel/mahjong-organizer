import hashlib
import io
import logging
import os
import re
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime

import json
from openpyxl import Workbook, load_workbook
from unidecode import unidecode

from django.conf import settings
from django.contrib.auth import logout
from django.db import transaction
from django.db.models import F
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, HttpResponseNotAllowed, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404
from django.template import loader
from django.utils.html import escape

from ..models import CeremonyState, Hand, Membership, Player, ScoreSheet, Seat, PublishedRound, Schedule, Screen, ScreenMode, Tenant
from ..signals import broadcast_display, broadcast_publish_state, invalidate_leaderboard
from .helpers import (
    BASE_DIR, get_counter, get_tenant, get_tournament, has_role,
    is_tenant_admin, json_body, set_counter, tenant_admin_required,
    tenant_role_required,
)
from .print_views import _country_flag
from .user_admin import TENANT_ROLES, reauth_ok, tenant_admin_and_reauthed

logger = logging.getLogger(__name__)

# Human labels for the tenant-role flags, shown in the user-management console.
TENANT_ROLE_LABELS = {'scorer': 'Scorer', 'display_op': 'Display operator', 'publisher': 'Publisher'}
from .restore_admin import list_backups
from .scoring import (
    scores_per_player_rows,
    scores_per_table_grid,
)


# Friendly labels for the editable tournament tournament, matching the field
# labels on the display admin page, so a rejected save names the field the way
# the operator sees it ("On-screen message", not "welcome").
_TOURNAMENT_LABELS = {
    "welcome": "On-screen message",
    "title": "Title",
    "fullname": "Full tournament name",
    "city": "City",
    "period": "Period",
    "rules": "Rules",
    "home_country": "Home nation",
    "countrycourt": "Federation code",
}

# Which TournamentSettings fields each page's set_tournament may write. An
# explicit allowlist (not a denylist): a display operator tuning layout must not
# be able to reach structural fields (nb_rounds, rules, has_teams, …), and no
# page may write the server-authoritative `counter`. The sets mirror the fields
# each page's form actually submits (the `tournament-input` controls).
DISPLAY_SETTINGS_FIELDS = frozenset({
    "rotation_time", "score_lines", "total_columns", "welcome", "zoom",
})
TOURNAMENT_SETTINGS_FIELDS = frozenset({
    "city", "countrycourt", "fullname", "has_teams", "home_country",
    "nb_rounds", "period", "rules", "title", "total_time",
})


def _screen_mode_or_404(tenant, raw_id):
    """Resolve a ScreenMode by id within ``tenant``, 404ing on a missing or
    non-numeric id (a stale button in a second tab, or a crafted request) rather
    than raising DoesNotExist/ValueError as a 500."""
    try:
        mode_id = int(raw_id)
    except (TypeError, ValueError):
        raise Http404("No such mode")
    return get_object_or_404(ScreenMode, tenant=tenant, id=mode_id)


def _name_is_round(name):
    """Guess whether an imported schedule row is a playing round from its name.

    Used only at import time to seed ``Schedule.is_round`` when the template
    omits the optional "Is round" column; staff can correct it afterwards in
    the settings UI.
    """
    name = name or ""
    return "round" in name.lower() or "session" in name.lower()


# Seating cells in the shipped template mirror the low half into the high half
# with formulas like "=14+8" (draw 22). openpyxl returns None for a formula cell
# unless the workbook carries Excel's cached result, and re-saving through
# openpyxl (as export does) drops that cache — so the seating sheet must be read
# formula-aware, evaluating the simple integer arithmetic here rather than
# treating an un-cached formula as an empty seat.
_SEAT_ARITH = re.compile(r'^\d+(\s*[+-]\s*\d+)*$')


def _seat_draw_number(raw):
    """The draw number seated in one seating cell. Accepts a literal number or a
    simple additive formula ("=14+8"); anything else (blank, or a formula we
    don't evaluate) is 0, the empty-seat sentinel, which the chart validation
    below then rejects for a full field."""
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).strip().lstrip('=')
    if not s:
        return 0
    if _SEAT_ARITH.match(s):
        # Only digits and +/- reached here, so summing the signed terms is safe.
        return sum(int(tok) for tok in re.findall(r'[+-]?\d+', s))
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return 0


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

    `views` is a JSON list of view strings in screen order. A mode is a full-room
    snapshot: applying it sets every screen, with screens beyond the saved list
    (added after the mode was saved) going blank. is_active mirrors that — the
    mode's padded views must equal every screen's current view, so a mode reads
    as active exactly when re-clicking it would be a no-op. (Initial paint only;
    JS keeps the highlight current after live edits.)"""
    current = [str(s.view) or "black" for s in screens]
    # Positional label (/1, /2…) plus the operator's name when the screen was renamed.
    labels = [f"/{i + 1}" + (f" — {s.friendly_name}" if s.friendly_name else "")
              for i, s in enumerate(screens)]
    out = []
    for mode in modes:
        views = mode.views if isinstance(mode.views, list) else []
        normalised = [v or "black" for v in views]
        padded = [normalised[i] if i < len(normalised) else "black"
                  for i in range(len(current))]
        out.append({
            "id": mode.id,
            "name": mode.name,
            "rows": [{"label": labels[i], "pretty": _pretty_view(padded[i])}
                     for i in range(len(current))],
            "views_json": json.dumps(normalised, separators=(',', ':')),
            "is_active": bool(current) and padded == current,
        })
    return out


def publisher_overview_rows(tenant, tournament):
    """Per-round summary for the Publisher overview page.

    One dict per round 1..nb_rounds with the counts shown in the table and the
    underlying per-table id lists, so the page can keep the aggregates accurate
    as it applies the same scorer.* / publish.state WebSocket deltas the Scoring
    page consumes (rather than re-querying on every keystroke). The sheet state
    mirrors the Scoring page badge exactly: a validated sheet is never also
    counted as in-progress (filled_keys already excludes validated tables).
    """
    nb_rounds = tournament.nb_rounds or 0

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


@tenant_admin_required
def admin_print_EMA(request):
    tournament = get_tournament(request)
    if tournament.rules == "MCR":
        wb = load_workbook(filename=str(BASE_DIR / "files/report_template.xlsx"), data_only=True)
        sheet_ranges = wb['MCR template']
    else:
        wb = load_workbook(filename=str(BASE_DIR / "files/report_template_Riichi.xlsx"), data_only=True)
        sheet_ranges = wb['Riichi template']
    standings = scores_per_player_rows(request, True)
    for row, player in enumerate(standings):
        items = [
            tournament.fullname,
            len(standings),
            player["pos"],
            player["first_name"],
            player["last_name"],
            player["EMA_ID"],
            player["total"]["tp"],
            player["total"]["mp"],
            "YES" if player["EMA_ID"] != "" else "NO",
            player["flag"].upper(),
            datetime.today().strftime('%d/%m/%Y'),
            tournament.countrycourt,   # Countrycourt: organising federation code (settings)
            tournament.city,
            2,
            tournament.title,
            tournament.rules,   # discipline column: "MCR" or "Riichi", not hardcoded
            tournament.period,
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


def _normalize_ema_id(raw):
    """The stored form of an EMA number: blank, or exactly eight digits.

    The id is optional — many competitors have none — so blank stays blank. A
    present-but-unparseable value is the organizer's mistake to surface, not to
    swallow: blanking it silently flips the EMA-report flag, and the EMA export
    reads the stored value back with ``int()``, so a free-text one crashes it.
    Zero-padded because that is the canonical form — an unpadded "1234" wouldn't
    match the same competitor imported from a template.

    Raises ``ValueError`` (via ``int``/``float``) for the caller to report in its
    own idiom: a template error on import, a 400 in the player editor.
    """
    if raw is None or not str(raw).strip():
        return ""
    return f"{int(float(str(raw).strip())):08d}"


def _ema_id_for_export(stored):
    """A stored EMA id as the template's number column wants it.

    Normally an int, since _normalize_ema_id keeps the field digits-only. A row
    predating that rule can still hold free text, so pass those through verbatim:
    dropping them would lose data out of what is meant to be a backup, and
    ``int()`` on them is what used to 500 the whole export. A re-import then
    reports the offending competitor by name.
    """
    if not stored:
        return None
    try:
        return int(stored)
    except (TypeError, ValueError):
        return stored


class TemplateImportError(Exception):
    """A validation problem in an uploaded tournament template that the organizer
    can fix (bad cell, wrong player count, missing seating sheet). Its message is
    shown to them verbatim; any other exception is treated as an unexpected error
    and reported with a traceback instead."""


@tenant_admin_required
def admin_upload_from_template(request):
    tenant = get_tenant(request)
    if request.method == 'POST':
        try:
            attached_file = request.FILES.get("myfile", None)
            if attached_file is None:
                return options(request)

            # One transaction for the whole wipe-and-load. The except below covers a
            # failure we can catch; this covers the one we can't — the worker being
            # killed or losing the database mid-import — which would otherwise commit a
            # genuinely half-imported tournament the organizer cannot tell from a good
            # one. The broadcasts stay outside it, so nothing is announced until the
            # import has actually committed.
            with transaction.atomic():
                Player.objects.filter(tenant=tenant).delete()

                # Read straight from the upload, never via a path on disk: any shared
                # staging file is a cross-tenant race, since two concurrent imports
                # would fight over it and one tenant could load the other's workbook
                # having already deleted its own players above.
                # data_only=False so the seating sheet's mirror formulas ("=14+8")
                # are read as formulas and evaluated below; a file with no cached
                # results would otherwise read those seats as empty. Options/Players/
                # Schedule hold plain values, so this doesn't change how they read.
                wb = load_workbook(attached_file, data_only=False, read_only=True)

                Schedule.objects.filter(tenant=tenant).delete()
                sched_sheet = wb['Schedule']
                schedule_rows = list(sched_sheet.iter_rows(min_row=2, max_col=4, values_only=True))
                # Column 4 ("Is round") is optional; when it is missing (an older or
                # hand-edited template leaves the cell None) fall back to guessing from
                # the name (a row named "Round N" / "Session N" is a playing round).
                # Either way staff can correct any misclassification afterwards in
                # Tournament settings.
                Schedule.objects.bulk_create([
                    Schedule(tenant=tenant, day=row[0], time=row[1], name=row[2],
                             is_round=bool(row[3]) if row[3] is not None else _name_is_round(row[2]))
                    for row in schedule_rows if row[0] is not None
                ])

                opt_sheet = wb['Options']
                opt_vals = [row[1] for row in opt_sheet.iter_rows(min_row=1, max_row=6, max_col=2, values_only=True)]
                tournament = get_tournament(request)
                tournament.fullname = opt_vals[0] or ""
                tournament.title = opt_vals[1] or ""
                # A blank/zero rounds count would create no seating at all and "succeed"
                # with nothing playable, so reject it. int() also normalises Excel's
                # float (5.0 -> 5) before it reaches iter_rows' max_row below.
                try:
                    nb_rounds = int(opt_vals[2])
                except (TypeError, ValueError):
                    nb_rounds = 0
                if nb_rounds < 1:
                    raise TemplateImportError(
                        "The Options sheet must set the number of rounds to at least 1 "
                        "(it was blank or zero) — otherwise no seating chart is created."
                    )
                tournament.nb_rounds = nb_rounds
                tournament.city = opt_vals[3] or ""
                tournament.period = opt_vals[4] or ""
                tournament.rules = opt_vals[5] or "MCR"
                tournament.save()

                # Player list: one Player per real person. The optional 'rand' column is a
                # pre-assigned draw number; when present the person is linked to their
                # seats below, otherwise the draw is made later (randomize / team draw).
                players_sheet = wb['Players']
                player_rows = list(players_sheet.iter_rows(min_row=2, max_col=6, values_only=True))
                player_objs = []
                any_team = False
                all_have_team = True
                for row in player_rows:
                    last_name_raw, first_name_raw, ema_raw, country, team_raw, rand_raw = row
                    # Skip fully-blank spacer / trailing rows and keep scanning: a row is
                    # a real competitor as long as it carries any name, so a mononym (only
                    # a first name, or only a surname) imports like everyone else and an
                    # interior blank doesn't truncate the list.
                    has_name = any(
                        isinstance(v, str) and v.strip() for v in (last_name_raw, first_name_raw)
                    )
                    if not has_name:
                        continue
                    # Keep the organizer's casing (strip only) — title-casing mangles
                    # "McDonald" and "van der Berg". The raw first/last are stored; the
                    # canonical display is "First Last".
                    first = first_name_raw.strip() if isinstance(first_name_raw, str) else ""
                    last = last_name_raw.strip() if isinstance(last_name_raw, str) else ""
                    full_name = f"{first} {last}".strip()
                    # Coerce to str (a numeric team cell would otherwise crash the whole
                    # import) and collapse internal whitespace, so "Team  A" and "Team A"
                    # aren't grouped as two different teams.
                    team = " ".join(str(team_raw).split()) if team_raw is not None else ""
                    if team:
                        any_team = True
                    else:
                        all_have_team = False
                    # The EMA id is optional (many competitors have none) -> blank
                    # silently. A present-but-unparseable value is the organizer's
                    # mistake to surface, not to swallow: blanking it also silently
                    # flips the EMA-report flag.
                    try:
                        ema = _normalize_ema_id(ema_raw)
                    except (TypeError, ValueError):
                        raise TemplateImportError(
                            f"Competitor '{full_name}' has an invalid EMA number "
                            f"'{ema_raw}'. Enter digits only, or leave the cell blank."
                        )
                    # The optional 'rand' column is the competitor's draw number. It is
                    # 1-based; 0 (or negative) collides with the empty-seat sentinel and
                    # would attach the player to every empty seat. The draw lives on the
                    # Player; seats are keyed by it. None until drawn.
                    if rand_raw is None:
                        draw_number = None
                    else:
                        try:
                            draw_number = int(rand_raw)
                        except (TypeError, ValueError):
                            raise TemplateImportError(
                                f"Competitor '{full_name}' has an invalid draw number "
                                f"'{rand_raw}'. Use a whole number from 1, or leave it blank."
                            )
                        if draw_number < 1:
                            raise TemplateImportError(
                                f"Competitor '{full_name}' has draw number {draw_number}; "
                                f"draw numbers start at 1."
                            )
                    player_objs.append(Player(
                        tenant=tenant, full_name=full_name, first_name=first,
                        last_name=last, EMA_ID=ema, country=country or "", team=team,
                        draw_number=draw_number,
                    ))
                if any_team and not all_have_team:
                    raise TemplateImportError(
                        "Some competitors have a team and some don't. When teams are "
                        "used every competitor must have one — fill in the blank team cells."
                    )
                # Teams are always groups of four. A team that comes out a different
                # size is a player-list mistake — most often a typo or case mismatch that
                # split one team in two ("Sweden" vs "sweden"), which the size check
                # catches without silently merging genuinely distinct names.
                if any_team:
                    sizes = Counter(p.team for p in player_objs)
                    wrong = sorted(name for name, n in sizes.items() if n != 4)
                    if wrong:
                        raise TemplateImportError(
                            f"Team '{wrong[0]}' has {sizes[wrong[0]]} competitor(s); every "
                            f"team must have exactly 4. Check for a typo or case mismatch "
                            f"in the team name."
                        )
                Player.objects.bulk_create(player_objs)

                # The player list is all-or-nothing on teams (enforced just above), so the
                # presence of any team name is the single signal for has_teams, which
                # gates team standings/columns/printouts everywhere.
                tournament.has_teams = any_team
                tournament.save()

                # Build the short display token: the first name alone, or first name +
                # the shortest surname prefix that separates competitors who share a
                # first name ("Chris D.", growing to "Chris Dere." if two share "Der").
                # bulk_create skips Player.save(), so set short_name here in one bulk_update.
                by_first = defaultdict(list)
                for player in player_objs:
                    by_first[unidecode(player.first_name).lower()].append(player)
                for group in by_first.values():
                    if len(group) == 1:
                        group[0].short_name = group[0].first_name
                        continue
                    for player in group:
                        if not player.last_name:
                            player.short_name = player.first_name
                            continue
                        n = 1
                        while n < len(player.last_name):
                            prefix = unidecode(player.last_name[:n]).lower()
                            if sum(unidecode(p.last_name[:n]).lower() == prefix
                                   for p in group) == 1:
                                break
                            n += 1
                        player.short_name = f"{player.first_name} {player.last_name[:n]}."
                Player.objects.bulk_update(player_objs, ['short_name'])

                Hand.objects.filter(tenant=tenant).delete()
                ScoreSheet.objects.filter(tenant=tenant).delete()
                Seat.objects.filter(tenant=tenant).delete()
                # The new schedule starts with empty scores, so any rounds that were
                # published for the previous tournament are now stale — unpublish them all.
                PublishedRound.objects.filter(tenant=tenant).delete()
                nb_players = len(player_objs)
                nb_tables = nb_players // 4
                # The seating sheet is optional: a workbook may carry only the player list,
                # schedule and settings, leaving the tournament without a chart until
                # one is built on the Seating page. When a "<N> players" sheet *is*
                # present it is read (and validated) here.
                sheet_name = '{0} players'.format(nb_players)
                if sheet_name in wb.sheetnames:
                    seating_sheet = wb[sheet_name]
                    # Materialize the full seating sheet once (rows 3..3+nb_rounds-1,
                    # cols 2..2+5*nb_tables-1). Each cell holds the draw number seated.
                    seating_rows = list(seating_sheet.iter_rows(
                        min_row=3, max_row=2 + tournament.nb_rounds,
                        min_col=2, max_col=1 + 5 * nb_tables,
                        values_only=True,
                    ))
                    seats_to_create = []
                    expected = set(range(1, nb_players + 1))
                    for round_idx, row in enumerate(seating_rows):
                        round_draws = []
                        for table_nb in range(nb_tables):
                            for wind in range(4):
                                draw_number = _seat_draw_number(row[wind + 5 * table_nb])
                                round_draws.append(draw_number)
                                seats_to_create.append(Seat(
                                    tenant=tenant,
                                    draw_number=draw_number,
                                    round_nb=round_idx + 1,
                                    table_nb=table_nb + 1,
                                    wind=wind + 1,
                                    minipoints=None,
                                    tablepoints=None,
                                ))
                        # A valid chart seats every competitor 1..N exactly once each
                        # round. A mismatch means the sheet is the wrong size, has blank
                        # cells, or (the classic case) carries formulas with no cached
                        # result — better to reject it than load a half-empty chart.
                        if sorted(round_draws) != sorted(expected):
                            raise TemplateImportError(
                                f"The '{sheet_name}' seating sheet is not a valid chart: "
                                f"round {round_idx + 1} does not seat each competitor "
                                f"1–{nb_players} exactly once. Check the sheet for blank "
                                f"cells, duplicate numbers, or the wrong field size."
                            )
                    Seat.objects.bulk_create(seats_to_create, batch_size=500)
                wb.close()
            invalidate_leaderboard(tenant.subdomain)
            broadcast_publish_state(tenant.subdomain, {'published_rounds': []})
        except Exception as exc:
            # Import is a full replace by design (it clears scores even on success),
            # and a half-imported tournament is worse than none — so any failure
            # leaves the tournament fully empty rather than half-loaded or silently
            # reverted to the old one. Wipe every player/seating/score table and
            # reset the settings the parse may have touched, so no half-branded
            # "ghost" tournament survives. The organizer fixes the file and retries.
            Player.objects.filter(tenant=tenant).delete()
            Hand.objects.filter(tenant=tenant).delete()
            ScoreSheet.objects.filter(tenant=tenant).delete()
            Seat.objects.filter(tenant=tenant).delete()
            PublishedRound.objects.filter(tenant=tenant).delete()
            Schedule.objects.filter(tenant=tenant).delete()
            tournament = get_tournament(request)
            tournament.fullname = ""
            tournament.title = ""
            tournament.nb_rounds = 0
            tournament.city = ""
            tournament.period = ""
            tournament.rules = "MCR"
            tournament.has_teams = False
            tournament.save()
            if isinstance(exc, TemplateImportError):
                # A validation problem we detected: show the actionable message.
                message = "Import failed — nothing was loaded.<br/>{0}".format(escape(str(exc)))
            else:
                # Unexpected error: keep the traceback to help diagnose it.
                message = "Error while creating tournament:<br/><code>{0}</code>".format(
                    escape(traceback.format_exc())
                )
            return options(request, error=message)

        return options(request)

    # GET (or any non-POST): show the settings page rather than falling through
    # to an implicit None return, which Django turns into a 500.
    return options(request)


@tenant_admin_required
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
    tournament = get_tournament(request)

    wb = Workbook()

    # Options: label in col A (for humans), value in col B (what the importer reads).
    opt_sheet = wb.active
    opt_sheet.title = 'Options'
    for row, (label, value) in enumerate([
        ('Competition name', tournament.fullname),
        ('Short name (initials)', tournament.title),
        ('Number of rounds', tournament.nb_rounds),
        ('City', tournament.city),
        ('Period', tournament.period),
        ('Rules', tournament.rules),
    ], start=1):
        opt_sheet.cell(row=row, column=1, value=label)
        opt_sheet.cell(row=row, column=2, value=value)

    # Players: id order is the original player-list row order (matches the draw exports).
    # Columns 1-6 mirror the shipped template.
    players = list(Player.objects.filter(tenant=tenant).order_by('id'))
    players_sheet = wb.create_sheet('Players')
    players_sheet.append([
        'Last name', 'First name', 'EMA number', 'Country',
        'Team name (optional)', 'Random position (1 - # of players)',
    ])
    for player in players:
        players_sheet.append([
            player.last_name,
            player.first_name,
            # EMA_ID is stored zero-padded ("00001234"); export the number so
            # _normalize_ema_id reproduces it on re-import (a blank stays blank).
            _ema_id_for_export(player.EMA_ID),
            player.country,
            player.team or None,
            player.draw_number,
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
    nb_players = len(players)
    nb_tables = nb_players // 4
    seats = list(Seat.objects.filter(tenant=tenant))
    if seats and nb_tables:
        seating_sheet = wb.create_sheet('{0} players'.format(nb_players))
        for table_nb in range(1, nb_tables + 1):
            base = 2 + 5 * (table_nb - 1)
            seating_sheet.cell(row=1, column=base, value='Table {0}'.format(table_nb))
            for wind, label in enumerate(['East', 'South', 'West', 'North']):
                seating_sheet.cell(row=2, column=base + wind, value=label)
        nb_rounds = max(tournament.nb_rounds, max(s.round_nb for s in seats))
        for round_nb in range(1, nb_rounds + 1):
            seating_sheet.cell(row=round_nb + 2, column=1, value='R{0}'.format(round_nb))
        for seat in seats:
            col = 2 + (seat.wind - 1) + 5 * (seat.table_nb - 1)
            seating_sheet.cell(row=seat.round_nb + 2, column=col, value=seat.draw_number)

    buf = io.BytesIO()
    wb.save(buf)
    # Keep the download name to a safe charset (the title is free text).
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (tournament.title or ""))
    filename = safe.strip("_") or "tournament"
    response = HttpResponse(
        buf.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = 'attachment; filename="{0}.xlsx"'.format(filename)
    return response


@tenant_admin_required
def admin_generate_seating(request):
    """Build the seating chart in-app for the current player list, instead of reading
    it from an Excel sheet — so a field size the template doesn't cover still gets
    a chart. Replaces the seating chart (and clears scores, which are keyed by
    seat) but keeps the player list, draw and schedule: the chart is independent of who
    sits where. Returns the quality measures as JSON for the page to display."""
    if request.method != 'POST':
        return HttpResponse('POST required', status=405)

    tenant = get_tenant(request)
    tournament = get_tournament(request)
    nb_players = Player.objects.filter(tenant=tenant).count()
    nb_rounds = tournament.nb_rounds or 0

    # Body carries the chosen method, a variation seed, and whether to apply (vs
    # just preview the measures). Absent body -> auto method, apply immediately.
    body = json_body(request)
    method = body.get('method', 'auto')
    try:
        seed = int(body.get('seed', 0))
    except (TypeError, ValueError):
        seed = 0
    try:
        # Number of best-effort attempts to search and keep the best of. A preview
        # searches many (default); applying a previewed chart passes tries=1 with
        # the winning seed to reproduce it exactly. Bounded so one request can't run
        # unboundedly (a wall-clock budget also caps it).
        tries = min(5000, max(1, int(body.get('tries', 400))))
    except (TypeError, ValueError):
        tries = 400
    do_apply = bool(body.get('apply', True))

    from .. import seating
    try:
        rows, meta = seating.generate(
            nb_players, nb_rounds, has_teams=tournament.has_teams,
            seed=seed, method=method, tries=tries)
    except seating.SeatingInfeasible as exc:
        return JsonResponse({'error': str(exc)}, status=400)

    m = seating.measure(rows, nb_players, nb_rounds, has_teams=tournament.has_teams)
    m['engine'] = meta['engine']
    payload = {'ok': True, 'applied': do_apply,
               'headline': seating.headline(m), 'measures': m,
               # The winning seed (best-effort) lets the page reproduce this exact
               # chart on apply, and reveals it to the organizer.
               'seed': meta.get('seed'), 'tries_run': meta.get('tries_run')}
    if not do_apply:
        # Preview only — the chart is deterministic for (method, seed), so applying
        # the same choice later reproduces exactly what was previewed.
        return JsonResponse(payload)

    with transaction.atomic():
        # Seating changed, so any entered scores/published rounds are stale.
        Hand.objects.filter(tenant=tenant).delete()
        ScoreSheet.objects.filter(tenant=tenant).delete()
        Seat.objects.filter(tenant=tenant).delete()
        PublishedRound.objects.filter(tenant=tenant).delete()
        Seat.objects.bulk_create([
            Seat(tenant=tenant, draw_number=draw_number, round_nb=round_nb,
                 table_nb=table_nb, wind=wind, minipoints=None, tablepoints=None)
            for (round_nb, table_nb, wind, draw_number) in rows
        ], batch_size=500)

    invalidate_leaderboard(tenant.subdomain)
    broadcast_publish_state(tenant.subdomain, {'published_rounds': []})
    return JsonResponse(payload)


@tenant_admin_required
def admin_team_draw(request):
    tenant = get_tenant(request)

    # The draw hands out seat numbers, so it needs a seating chart to draw for.
    # Without one every drawn number would be rejected at save time (after the
    # whole ceremony), so stop up front with a clear message instead.
    if not Seat.objects.filter(tenant=tenant).exists():
        template = loader.get_template('mahj/admin_seating_required.html')
        return HttpResponse(template.render(
            {'draw_title': 'Team Draw', 'draw_kind': 'team draw'}, request))

    players = list(Player.objects.filter(tenant=tenant).order_by('full_name'))

    # id order is the original row order of the Excel "Players" sheet. The CSV
    # export sorts on this so the drawn numbers line up with the template rows.
    order = {p.id: i for i, p in enumerate(sorted(players, key=lambda p: p.id), start=1)}

    teams_dict = {}
    for p in players:
        if p.team:
            teams_dict.setdefault(p.team, []).append({
                "id": p.id,
                "original_index": order[p.id],
                "full_name": p.full_name,
                "short_name": p.short_name,
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
    drawn = [p for p in players if p.draw_number is not None and p.team]

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
    # The page reads these through |json_script, which escapes the markup
    # characters json.dumps leaves alone — team and player names are
    # operator-entered, so a name containing `</script>` would inject script.
    context = {
        "teams": teams_list,
        "nb_teams": nb_teams,
        "saved_draw": saved_draw,
    }
    return HttpResponse(template.render(context, request))


@tenant_admin_required
def admin_team_draw_save(request):
    """Save a completed team draw: give each competitor the number they drew.

    The whole draw is replaced at once (the operator runs it as one ceremony), so
    the batch is validated before anything is written: every drawn number must be
    a real seat and no competitor or number may appear twice. Only then does the
    reassignment run, inside a single transaction, so a mid-write failure rolls
    back instead of leaving the draw half-wiped."""
    if request.method != 'POST':
        return HttpResponse('POST required', status=405)

    tenant = get_tenant(request)
    try:
        assignments = json_body(request)['assignments']  # [{player_id, rand_id}]
        player_ids = [a['player_id'] for a in assignments]
        draw_numbers = [a['rand_id'] for a in assignments]  # rand_id = the drawn number
    except (TypeError, KeyError):
        return HttpResponse('Malformed request', status=400)

    if len(set(player_ids)) != len(player_ids) or len(set(draw_numbers)) != len(draw_numbers):
        return HttpResponse('Each competitor and each draw number must appear once', status=400)

    valid = set(Seat.objects.filter(tenant=tenant).values_list('draw_number', flat=True))
    unknown = sorted(set(draw_numbers) - valid)
    if unknown:
        return HttpResponse('#{0} is not a valid seat'.format(unknown[0]), status=400)

    players = {p.id: p for p in Player.objects.filter(tenant=tenant, id__in=player_ids)}
    if len(players) != len(player_ids):
        return HttpResponse('Player list changed — reload and draw again', status=400)

    # Free the target draw numbers from any current holder and clear these
    # competitors' current numbers, then assign each their drawn number. Clearing
    # first keeps the per-tenant unique draw_number constraint satisfied.
    with transaction.atomic():
        Player.objects.filter(tenant=tenant, draw_number__in=draw_numbers).update(draw_number=None)
        Player.objects.filter(tenant=tenant, id__in=player_ids).update(draw_number=None)
        for a in assignments:
            players[a['player_id']].draw_number = a['rand_id']
        Player.objects.bulk_update(players.values(), ['draw_number'])

    return HttpResponse('OK')


@tenant_admin_required
def admin_player_draw(request):
    """Live individual draw: competitors arrive one at a time, physically draw a
    number, and the operator types it in. Each assignment is saved immediately
    (via admin_player_draw_assign) so the seating chart is live as the desk runs.
    The draw is recorded as Player.draw_number, same as Randomize / Team draw."""
    tenant = get_tenant(request)

    # No seating chart means no seat numbers to hand out, and every assignment
    # would be rejected by admin_player_draw_assign. Stop up front instead.
    if not Seat.objects.filter(tenant=tenant).exists():
        template = loader.get_template('mahj/admin_seating_required.html')
        return HttpResponse(template.render(
            {'draw_title': 'Player Draw', 'draw_kind': 'individual draw'}, request))

    all_players = list(Player.objects.filter(tenant=tenant).order_by('full_name'))

    # id order is the original row order of the Excel "Players" sheet, so a CSV
    # export lines up with the template rows (matches the team-draw export).
    order = {p.id: i for i, p in enumerate(sorted(all_players, key=lambda p: p.id), start=1)}

    players = [{
        "id": p.id,
        "original_index": order[p.id],
        "full_name": p.full_name,
        "short_name": p.short_name,
        "country": p.country,
        "flag": _country_flag(p.country),
        "draw_number": p.draw_number,
    } for p in all_players]

    draw_numbers = sorted({s.draw_number for s in Seat.objects.filter(tenant=tenant)})

    template = loader.get_template('mahj/admin_player_draw.html')
    # |json_script in the template, not json.dumps here — see admin_team_draw.
    context = {
        "players": players,
        "draw_numbers": draw_numbers,
    }
    return HttpResponse(template.render(context, request))


@tenant_admin_required
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
    data = json_body(request)
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
_PLAYER_EDITABLE_FIELDS = ['first_name', 'last_name', 'EMA_ID', 'country', 'team']


@tenant_admin_required
def player_editor_save(request):
    """Persist inline edits from the Player editor table. Accepts JSON
    ``{"players": [{"id", <field>...}]}`` and bulk-updates the editable
    metadata; unknown ids are ignored, over-long values are rejected up front."""
    if request.method != 'POST':
        return HttpResponse('POST required', status=405)

    tenant = get_tenant(request)
    rows = json_body(request).get('players', [])
    if not isinstance(rows, list):
        return HttpResponse('Malformed request body', status=400)
    rows = [r for r in rows if isinstance(r, dict)]
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
            if field == 'EMA_ID':
                # Same rule as the importer, so an edited id matches an imported one
                # and the EMA export's int() can't meet free text.
                try:
                    value = _normalize_ema_id(value)
                except (TypeError, ValueError):
                    return HttpResponse(
                        f"'{value}' is not a valid EMA number — enter digits only, "
                        f"or leave it blank.", status=400)
            max_length = Player._meta.get_field(field).max_length
            if max_length and len(value) > max_length:
                return HttpResponse(
                    f"{field} is too long: {len(value)} characters "
                    f"(maximum {max_length}).", status=400)
            setattr(player, field, value)
        # First/last are the edited fields; when either changed keep the canonical
        # "First Last" display and the short token in sync (bulk_update bypasses the
        # model's save()). short_name loses cross-field disambiguation on an edit —
        # acceptable for the occasional typo fix; a re-import rebuilds it fully.
        if 'first_name' in r or 'last_name' in r:
            player.full_name = f"{player.first_name} {player.last_name}".strip()
            player.short_name = player.first_name
        to_update.append(player)

    if to_update:
        Player.objects.bulk_update(
            to_update, _PLAYER_EDITABLE_FIELDS + ['full_name', 'short_name'])
    return HttpResponse('OK')


# Public (display screens are public, like /scan and counter_start): serve the
# tenant's uploaded logo. Templates fall back to the static mcr_logo when unset,
# so this is only hit when a logo exists.
def logo(request):
    tournament = get_tournament(request)
    if not tournament.logo:
        raise Http404
    resp = HttpResponse(bytes(tournament.logo), content_type="image/png")
    resp["Cache-Control"] = "public, max-age=86400"
    resp["ETag"] = f'"{tournament.logo_etag}"'
    return resp


@tenant_admin_required
def update_logo(request):
    tournament = get_tournament(request)
    if request.POST.get("reset") == "1":
        tournament.logo = None
        tournament.logo_etag = ""
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
        tournament.logo = data
        tournament.logo_etag = hashlib.md5(data).hexdigest()
    # Scope the write to the logo fields: a full-row save would also persist this
    # (possibly stale) instance's `counter`, which could stop a running round
    # timer. signals.py still invalidates the cached settings on post_save.
    tournament.save(update_fields=['logo', 'logo_etag'])
    return HttpResponse("OK")


@tenant_admin_and_reauthed
def admin_reset(request):
    """Factory-reset the tournament: wipe every tenant row and its settings.

    Admin-only, POST-only, and re-auth gated (``@tenant_admin_and_reauthed``): the
    operator must have confirmed their password recently, so a stale/borrowed
    session can't drive this irreversible wipe directly. The UI adds a
    type-to-confirm + confirm dialog on top. It clears all tournament data (player list, seating,
    hands, score sheets, published rounds, schedule) *and* the tenant's
    configuration (title/branding/format, logo, screens, screen modes, publish
    target, ceremony state), leaving a blank instance ready for a fresh import.
    Deleting the TournamentSettings row lets get_tournament recreate it at defaults;
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
        # round timer to defaults; get_tournament recreates a fresh one on next read.
        TournamentSettings.objects.filter(tenant=tenant).delete()
    # Wake public displays and scorer pages: nothing is published anymore, the
    # screen set is gone, and the leaderboard/settings caches are stale.
    invalidate_leaderboard(tenant.subdomain)
    broadcast_publish_state(tenant.subdomain, {'published_rounds': []})
    broadcast_display(tenant.subdomain, 'screen.update', {'event': 'screens_changed'})
    return JsonResponse({'status': 'ok'})


@tenant_admin_required
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


@tenant_role_required('scorer', 'display_op', 'publisher')
def publish_status(request):
    """Poll the running (or last) publish job — drives the shell progress toast.
    Any admin role can read it (auto-publish fires on a publisher's round publish,
    not just staff's manual push). Returns {phase, pct, message, error}; phase is
    idle when nothing is running."""
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    from ..publish.trigger import get_progress
    return JsonResponse(get_progress(subdomain) or {'phase': 'idle'})


@tenant_admin_required
def publish_target_save(request):
    """Save this tenant's SFTP publish target (tenant admin only).

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
    tournament = get_tournament(request)
    public_url = request.POST.get('public_url', '').strip()
    if public_url != tournament.public_url:
        tournament.public_url = public_url
        tournament.save(update_fields=['public_url'])  # signals bust the cache
    return JsonResponse({'status': 'ok'})


@tenant_admin_required
def publish_target_test(request):
    """Open + close an SFTP connection using the values currently in the form —
    not the saved target — so staff can verify before saving. A blank password/
    key field falls back to the stored secret, so you can test an unchanged
    credential without re-typing it.

    This does let a tenant admin make the server open a TCP connection to any
    host:port they type. That is inherent to a "test this target" button and the
    role is trusted, so it stays — but each attempt is logged with the user and
    target, so the probing is at least attributable after the fact."""
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

    # No `subdomain`: these are unsaved form values, so _connect must not learn
    # and pin a host key from them onto the stored target. PublishConfig.subdomain
    # defaults to '' and _remember_host_key skips on that — keep it that way.
    cfg = PublishConfig(
        host=host, port=port,
        username=request.POST.get('username', '').strip(),
        path=request.POST.get('path', '').strip() or '.',
        password=password,
        key_data=key,
        host_key=request.POST.get('host_key', '').strip(),
    )
    logger.info("publish target test by %s (tenant %s) -> %s:%s",
                request.user, tenant.subdomain if tenant else '-', cfg.host, cfg.port)
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
        if request.method != 'POST' or not has_role(request, 'display_op'):
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


def _apply_set_tournament(request, tournament, allowed_fields):
    """Persist ``?tournament-<field>=<value>`` params onto the tenant settings and
    return the response to send back. Shared by the Display page (screen-layout
    tuning) and the Tournament settings page (identity + round length), each of
    which passes its own ``allowed_fields`` set — anything not in it is ignored,
    so a display operator can't reach structural or identity fields and no page
    can touch the server-authoritative `counter`."""
    touched_fields = []
    for var in request.GET.keys():
        if var.startswith("tournament-"):
            field = var[len("tournament-"):]
            if field not in allowed_fields:
                continue
            if hasattr(tournament, field):
                value = request.GET.get(var)
                # Coerce booleans: every GET value is a string, and a non-empty
                # string ("false") is truthy — so a raw setattr would store True
                # for both. Map the usual truthy spellings instead.
                if tournament._meta.get_field(field).get_internal_type() == 'BooleanField':
                    value = value.strip().lower() in ('true', '1', 'on', 'yes')
                setattr(tournament, field, value)
                touched_fields.append(field)
    if touched_fields:
        # Reject over-long text here, before it reaches the DB: on
        # PostgreSQL an oversized value raises a bare 500, which the admin
        # UI showed silently. Returning a readable 400 instead lets the
        # page surface exactly which field was too long, and why.
        for field in touched_fields:
            max_length = getattr(tournament._meta.get_field(field), "max_length", None)
            value = getattr(tournament, field)
            if max_length and isinstance(value, str) and len(value) > max_length:
                label = _TOURNAMENT_LABELS.get(field, field)
                return HttpResponse(
                    f"{label} is too long: {len(value)} characters "
                    f"(maximum {max_length}).",
                    status=400)
        # Scope the write to the fields actually edited: a full-row save would
        # also persist this instance's `counter`, which could stop a running
        # round timer. signals.py still busts the cache on post_save.
        try:
            tournament.save(update_fields=touched_fields)
        except Exception as exc:
            # Any other save failure (e.g. a non-numeric value for a number
            # field) — return the reason rather than a silent 500.
            return HttpResponse(f"Could not save: {exc}", status=400)
    return HttpResponse(str(tournament))


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


@tenant_role_required('scorer', 'display_op', 'publisher')
def options(request, error=None):
    tenant = get_tenant(request)
    if request.GET.get('logout') == "1":
        # Logout is a state change, so it must be POST (a GET link would let a
        # crafted cross-site navigation log the operator out mid-tournament).
        if request.method != 'POST':
            return HttpResponseNotAllowed(['POST'])
        logout(request)
        return HttpResponseRedirect('admin')

    template = loader.get_template('mahj/admin.html')
    page = request.GET.get('page')
    
    # Land each single-role account on the page it works from; tenant admins get
    # the full dashboard (welcome), so only redirect non-admins.
    if page is None and not is_tenant_admin(request):
        if has_role(request, 'scorer'):
            page = "scoring"
        elif has_role(request, 'display_op'):
            page = "display"
        elif has_role(request, 'publisher'):
            # Publishers manage publishing from the scoring page.
            page = "scoring"

    if page == "welcome" or page is None:
        page = "welcome"
        from ..publish.sftp_upload import is_configured as _static_publish_configured
        from ..scoring import _last_complete_round, publish_state
        tournament = get_tournament(request)
        nb_players = Player.objects.filter(tenant=tenant).count()
        nb_drawn = Player.objects.filter(tenant=tenant, draw_number__isnull=False).count()
        nb_screens = Screen.objects.filter(tenant=tenant).count()
        # _last_complete_round returns nb_rounds when it finds no incomplete seat —
        # which also happens with no seats at all, a false "all complete". Guard on
        # the seating chart actually existing.
        has_seats = Seat.objects.filter(tenant=tenant).exists()
        complete_round = _last_complete_round(tenant, tournament) if has_seats else 0
        last_published, _ = publish_state(tenant, tournament)
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
                "tournament": tournament,
                "nb_players": nb_players,
                # Whether a seating chart exists at all (imported or generated) —
                # the player list can be drawn in only once there are seats to fill.
                "has_seats": has_seats,
                # A player is "drawn in" once assigned a draw number; the player list is
                # ready to play when every player holds one.
                "draw_done": nb_players > 0 and nb_drawn == nb_players,
                "nb_drawn": nb_drawn,
                "nb_screens": nb_screens,
                "complete_round": complete_round,
                "last_published": last_published,
                "schedule_rounds": schedule_rounds,
                "schedule_round_mismatch": schedule_total > 0 and schedule_rounds != tournament.nb_rounds,
                # Server-authoritative round timer: >0 and in the future means a
                # round is counting down / running (the dashboard shows it live).
                "counter": get_counter(tenant),
            },
            request,
        )
    elif page == "display" and not has_role(request, 'display_op'):
        # Display-operator-only: the shared admin gate admits any app role, so a
        # scorer/publisher must be turned away here — otherwise they could not
        # only view this page but drive its inline mutating actions below
        # (add/remove screen, set_tournament, set_all_views, set_mode…).
        page_content = "None"
    elif page == "display":
        tournament = get_tournament(request)
        action = request.GET.get('action')
        # Every branch below mutates state (or fires a broadcast), so it must be
        # POST: a GET link would let a crafted cross-site navigation drive these
        # (delete a screen, blank every projector, rewrite settings) while a
        # display operator is logged in. Requiring POST also hands CSRF
        # protection back to Django's middleware, which never checks GET.
        is_mutation = (
            action in {"set_tournament", "add_screen", "remove_screen",
                       "identify_screens", "add_mode", "set_all_views"}
            or request.GET.get('rm_mode') or request.GET.get('set_mode'))
        if is_mutation and request.method != 'POST':
            return HttpResponseNotAllowed(['POST'])
        if action == "set_tournament":
            return _apply_set_tournament(request, tournament, DISPLAY_SETTINGS_FIELDS)
        elif action == "add_screen":
            Screen(tenant=tenant, name="", view="black").save()
            # 'screens_changed' (not plain 'screen_update') so the overview grid
            # redraws for the new screen count. Existing per-screen displays are
            # unaffected: only the last position is ever added or removed.
            broadcast_display(tenant.subdomain, 'screen.update', {'event': 'screens_changed'})
            return HttpResponseRedirect('admin?page=display#configure-screens')
        elif action == "remove_screen":
            last = Screen.objects.filter(tenant=tenant).order_by('id').last()
            if last is not None:
                last.delete()
                broadcast_display(tenant.subdomain, 'screen.update', {'event': 'screens_changed'})
            return HttpResponseRedirect('admin?page=display#configure-screens')
        elif action == "identify_screens":
            # Flash each screen's positional number (/1, /2, …) as a corner badge
            # for a few seconds so an operator can match physical projectors to
            # their URLs. Reuses the existing 'screen.update' channel with a
            # distinct event the display socket intercepts without reloading —
            # see mahj/static/js/display_socket.js.
            broadcast_display(tenant.subdomain, 'screen.update', {'event': 'screen_identify'})
            return HttpResponse("")
        elif action == "add_mode":
            mode_name = request.POST.get('mode_name')
            screens = Screen.objects.filter(tenant=tenant).order_by('id')
            views_list = [str(screen.view) for screen in screens]
            ScreenMode(tenant=tenant, name=mode_name, views=views_list).save()
            return HttpResponseRedirect('admin?page=display#configure-screens')
        elif request.GET.get('rm_mode'):
            mode = _screen_mode_or_404(tenant, request.GET.get('rm_mode'))
            mode.delete()
            return HttpResponseRedirect('admin?page=display')
        elif action == "set_all_views":
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
            mode = _screen_mode_or_404(tenant, request.GET.get('set_mode'))
            views_list = mode.views if isinstance(mode.views, list) else []
            screens = Screen.objects.filter(tenant=tenant).order_by('id')
            applied = []
            # A mode is a full-room snapshot (add_mode saves every screen's
            # view), so applying one sets every screen: a screen added after
            # the mode was saved goes blank rather than keeping stale content
            # (e.g. live standings during a "Break" mode).
            for i, screen in enumerate(screens):
                screen.view = views_list[i] if i < len(views_list) else "black"
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
            "tournament": tournament,
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
        if not is_tenant_admin(request):
            page_content = "None"
        else:
            tournament = get_tournament(request)
            action = request.GET.get('action')
            if action in ("set_tournament", "save_schedule") and request.method != 'POST':
                return HttpResponseNotAllowed(['POST'])
            if action == "set_tournament":
                return _apply_set_tournament(request, tournament, TOURNAMENT_SETTINGS_FIELDS)
            if action == "save_schedule":
                return _save_schedule(request, tenant)
            template2 = loader.get_template('mahj/admin_settings.html')
            schedule_rows = [
                {"day": s.day or "", "time": s.time or "",
                 "name": s.name or "", "is_round": s.is_round}
                for s in Schedule.objects.filter(tenant=tenant).order_by('id')
            ]
            page_content = template2.render({
                "tournament": tournament,
                "schedule_rows": schedule_rows,
            }, request)
    elif page == "player_editor":
        # Staff-only: a scorer/display op reaching ?page=player_editor gets nothing.
        if not is_tenant_admin(request):
            page_content = "None"
        else:
            players = Player.objects.filter(tenant=tenant).order_by(
                F('draw_number').asc(nulls_last=True), 'full_name')
            player_rows = [
                {'id': p.id, 'draw_number': p.draw_number, 'full_name': p.full_name,
                 'first_name': p.first_name, 'last_name': p.last_name,
                 'EMA_ID': p.EMA_ID, 'country': p.country, 'team': p.team}
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
        if not is_tenant_admin(request):
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
                "public_url": get_tournament(request).public_url,
                "subdomain": tenant.subdomain if tenant else '',
            }, request)
    elif page == "import_template" and not is_tenant_admin(request):
        # Tenant-admin-only: this page erases and re-imports the whole tournament.
        page_content = "None"
    elif page == "import_template":
        # The upload confirm dialog names what it will erase, so tell the fragment
        # how big the current tournament is and whether any scores exist.
        existing_players = Player.objects.filter(tenant=tenant).count()
        existing_scores = (
            Seat.objects.filter(tenant=tenant, minipoints__isnull=False).exists()
            or Hand.objects.filter(tenant=tenant).exists()
        )
        template2 = loader.get_template('mahj/admin_import_template.html')
        page_content = template2.render({
            'existing_players': existing_players,
            'existing_scores': existing_scores,
        }, request)
    elif page == "seating" and not is_tenant_admin(request):
        # Tenant-admin-only: generating replaces the chart and clears scores.
        page_content = "None"
    elif page == "seating":
        from .. import seating as _seating
        tournament = get_tournament(request)
        nb_players = Player.objects.filter(tenant=tenant).count()
        nb_rounds = tournament.nb_rounds or 0
        # Measure the seating chart currently in place (independent of the player list:
        # its size is the draw slots it seats), so the page can show what exists.
        seats = list(Seat.objects.filter(tenant=tenant)
                     .values_list('round_nb', 'table_nb', 'wind', 'draw_number'))
        current = current_headline = None
        if seats:
            n_chart = len({s[3] for s in seats})
            r_chart = len({s[0] for s in seats})
            try:
                current = _seating.measure(seats, n_chart, r_chart,
                                           has_teams=tournament.has_teams)
                current_headline = _seating.headline(current)
                current['headline'] = current_headline
            except Exception:
                current = None
        can_generate = (nb_players >= 8 and nb_players % 4 == 0 and nb_rounds >= 1)
        seating_scores = (
            Seat.objects.filter(tenant=tenant, minipoints__isnull=False).exists()
            or Hand.objects.filter(tenant=tenant).exists()
        )
        template2 = loader.get_template('mahj/admin_seating.html')
        page_content = template2.render({
            'nb_players': nb_players,
            'nb_rounds': nb_rounds,
            'has_teams': tournament.has_teams,
            'has_seats': bool(seats),
            'existing_scores': seating_scores,
            'current': current,
            'current_headline': current_headline,
            'can_generate': can_generate,
            'algebraic_ok': can_generate and _seating.algebraic_feasible(nb_players, nb_rounds),
        }, request)
    elif page == "scoring" and not has_role(request, 'scorer', 'publisher'):
        # Scoring is for scorers and publishers (a display operator has no reason
        # to see it); the shared admin gate admits display ops too, so exclude them.
        page_content = "None"
    elif page == "scoring":
        tournament = get_tournament(request)
        grid = scores_per_table_grid(request)
        all_players = Player.objects.filter(tenant=tenant).order_by('full_name')
        try:
            nb_rounds = len(scores_per_player_rows(request, True)[0]["scores"])
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
            'grid': grid,
            "players": all_players,
            "tournament": tournament,
            "active_round": nb_rounds + 1,
            "published_rounds": published_rounds,
            "subdomain": tenant.subdomain if tenant else '',
            "validated_keys": validated_keys,
            "filled_keys": filled_keys,
            # Only publishers (and tenant admins) may publish/unpublish — the
            # endpoint is gated the same way, so keep the toggle disabled for plain
            # scorer accounts to avoid a dead control.
            "can_publish": has_role(request, 'publisher'),
        }
        page_content = template2.render(context, request)
    elif page == "ceremony" and not has_role(request, 'display_op'):
        # Display-operator-only, like the display page (ceremony takes over the
        # screens); its data/control endpoints are already display_op-gated.
        page_content = "None"
    elif page == "ceremony":
        template2 = loader.get_template('mahj/admin_ceremony.html')
        page_content = template2.render({
            "tournament": get_tournament(request),
            "subdomain": tenant.subdomain if tenant else '',
            "screens": Screen.objects.filter(tenant=tenant).order_by('id'),
            # Same-origin base for the preview iframes (see the display page):
            # cloud → https://<tenant>.<BASE_DOMAIN>, standalone → http://<host>:<port>.
            "screen_base": request.build_absolute_uri('/').rstrip('/'),
        }, request)
    elif page == "publisher_overview":
        # Publisher-only: a plain scorer reaching this page (?page=…) gets nothing.
        # has_role('publisher') also covers tenant admins and superusers.
        if not has_role(request, 'publisher'):
            page_content = "None"
        else:
            tournament = get_tournament(request)
            template2 = loader.get_template('mahj/admin_publisher_overview.html')
            page_content = template2.render({
                "rows": publisher_overview_rows(tenant, tournament),
                "tournament": tournament,
                "subdomain": tenant.subdomain if tenant else '',
            }, request)
    elif page == "users":
        # Tenant-admin-only, scoped to THIS tenant: a scorer/display op reaching
        # ?page=users gets nothing.
        if not is_tenant_admin(request):
            page_content = "None"
        elif not reauth_ok(request):
            # Borrowed/unattended session: make the admin re-enter their password
            # before exposing (or letting them touch) user management. Link-only
            # admins have no password to confirm, so they're shut out entirely.
            template2 = loader.get_template('mahj/admin_users_reauth.html')
            page_content = template2.render(
                {"link_only": not request.user.has_usable_password()}, request)
        else:
            # Only this tenant's memberships — other tenants' users are invisible.
            memberships = (Membership.objects.filter(tenant=tenant)
                           .select_related('user').order_by('user__username'))
            # A user is "shared" (credential-managed only by a superuser) if they
            # also belong to another tenant. One query, not one per row.
            shared_user_ids = set(
                Membership.objects.exclude(tenant=tenant)
                .filter(user__in=[m.user_id for m in memberships])
                .values_list('user_id', flat=True))
            user_rows = []
            for m in memberships:
                u = m.user
                user_rows.append({
                    "id": u.id,
                    "username": u.username,
                    "is_tenant_admin": m.is_tenant_admin,
                    "is_self": u.id == request.user.id,
                    "last_login": u.last_login,
                    "has_password": u.has_usable_password(),
                    "shared": u.id in shared_user_ids,
                    "roles": [{"name": n, "label": TENANT_ROLE_LABELS[n],
                               "active": getattr(m, f'is_{n}')} for n in TENANT_ROLES],
                })
            template2 = loader.get_template('mahj/admin_users.html')
            page_content = template2.render({
                "user_rows": user_rows,
                "role_defs": [{"name": n, "label": TENANT_ROLE_LABELS[n]} for n in TENANT_ROLES],
                "link_validity_days": settings.SESAME_MAX_AGE // 86400,
            }, request)
    elif page == "tenants":
        # Superuser-only: create/rename tenants. Meaningless in the single-tenant
        # standalone build (the tenant is pinned via LOCAL_TENANT), so it's hidden
        # there.
        if not request.user.is_superuser or settings.STANDALONE:
            page_content = "None"
        elif not reauth_ok(request):
            template2 = loader.get_template('mahj/admin_users_reauth.html')
            page_content = template2.render(
                {"link_only": not request.user.has_usable_password(),
                 "reauth_next": "tenants"}, request)
        else:
            tenant_rows = [
                {"id": t.id, "name": t.name, "subdomain": t.subdomain,
                 "admins": Membership.objects.filter(tenant=t, is_tenant_admin=True).count(),
                 "members": Membership.objects.filter(tenant=t).count()}
                for t in Tenant.objects.all().order_by('subdomain')
            ]
            template2 = loader.get_template('mahj/admin_tenants.html')
            page_content = template2.render({
                "tenant_rows": tenant_rows,
                "base_domain": settings.BASE_DOMAIN,
            }, request)
    elif page == "database_restore":
        # Superuser-only (the restore is whole-cluster, a platform-operator
        # action — see restore_admin), and — like user management — re-confirm the
        # password first: this page can WIPE the live DB, so a borrowed session
        # must re-auth.
        if not request.user.is_superuser:
            page_content = "None"
        elif not reauth_ok(request):
            template2 = loader.get_template('mahj/admin_users_reauth.html')
            page_content = template2.render(
                {"link_only": not request.user.has_usable_password(),
                 "reauth_next": "database_restore"}, request)
        else:
            # Counts the confirm dialog shows as "what you're about to overwrite".
            # Unscoped (whole-DB): a restore replaces every tenant's rows at once,
            # matching the worker's post-restore report.
            db_counts = {
                "players": Player.objects.count(),
                "seats": Seat.objects.count(),
                "hands": Hand.objects.count(),
            }
            if settings.STANDALONE:
                from .. import standalone_backup
                restore_ctx = {
                    "groups": standalone_backup.list_snapshot_groups(),
                    "db_name": standalone_backup.CONFIRM_TOKEN,
                    "pull_configured": False,   # off-host pull is Postgres-only
                    "standalone": True,         # restore applies on relaunch
                    "db_counts": db_counts,
                }
            else:
                restore_ctx = {
                    "groups": list_backups(),
                    "db_name": settings.DATABASES['default']['NAME'],
                    "pull_configured": bool(os.environ.get('REMOTE')),
                    "db_counts": db_counts,
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
        # user_is_scorer / user_is_display_op / user_is_publisher / is_tenant_admin
        # come from the role_flags context processor (tenant-scoped).
        "uses_teams": get_tournament(request).has_teams,
        # Standalone is single-tenant (pinned via LOCAL_TENANT), so the superuser
        # tenant-management page is meaningless there — hide it.
        "standalone": settings.STANDALONE,
        # Drives the shell-wide publish progress toast (polls publish_status) — only
        # when web publishing is configured, so idle installs don't poll.
        "static_publish_enabled": _static_publish_configured(tenant.subdomain if tenant else ''),
    }
    return HttpResponse(template.render(context, request))
