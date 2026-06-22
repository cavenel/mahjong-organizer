import io
import os
import time
import traceback
from datetime import datetime

import simplejson as json
from openpyxl import load_workbook
from unidecode import unidecode

from django.contrib.auth import logout
from django.contrib.auth.decorators import user_passes_test
from django.core.files.storage import FileSystemStorage
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseRedirect, JsonResponse
from django.template import loader

from ..models import Hand, Player, Player_data, Position, PublishedRound, Schedule, Screen, ScreenMode
from ..signals import broadcast_display, broadcast_publish_state, invalidate_leaderboard
from .helpers import BASE_DIR, can_access_admin, get_counter, get_tenant, get_variables, is_display_op, is_publisher, is_scorer, is_scorer_or_display_op, player_statistics, set_counter
from .print_views import _country_flag
from .scoring import (
    scores_per_player_json,
    scores_per_table_json,
    stat_all_rounds,
    stat_rounds,
)


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
            "SE",
            variables.city,
            2,
            variables.title,
            "Riichi",
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
            Player_data.objects.filter(tenant=tenant).delete()

            wb = load_workbook(filename=str(tmp_file), data_only=True, read_only=True)

            Schedule.objects.filter(tenant=tenant).delete()
            sched_sheet = wb['Schedule']
            schedule_rows = list(sched_sheet.iter_rows(min_row=2, max_col=3, values_only=True))
            Schedule.objects.bulk_create([
                Schedule(tenant=tenant, day=row[0], time=row[1], name=row[2])
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

            players_sheet = wb['Players']
            player_rows = list(players_sheet.iter_rows(min_row=2, max_col=6, values_only=True))
            player_data_objs = []
            player_objs = []
            any_team = False
            all_have_team = True
            for idx, row in enumerate(player_rows, start=1):
                if row[0] is None:
                    break
                last_name_raw, first_name_raw, ema_raw, country, team_raw, rand_raw = row
                # Make first_name, last_name into title-case strings:
                if isinstance(first_name_raw, str):
                    first_name_raw = first_name_raw.strip().title()
                else:
                    first_name_raw = ""
                if isinstance(last_name_raw, str):
                    last_name_raw = last_name_raw.strip().title()
                else:
                    last_name_raw = ""
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
                player_data_objs.append(Player_data(
                    tenant=tenant, full_name=full_name, EMA_ID=ema,
                    country=country, email="", team=team,
                ))
                if rand_raw is None:
                    player_objs.append(Player(
                        tenant=tenant, full_name="Player #" + str(idx),
                        EMA_ID="", country="", email="", rand_id=idx, team="",
                    ))
                else:
                    player_objs.append(Player(
                        tenant=tenant, full_name=full_name, EMA_ID=ema,
                        country=country, email="", rand_id=int(rand_raw), team=team,
                    ))
            if any_team and not all_have_team:
                raise ValueError("All players must have a team when teams are used, but some team cells are empty.")
            Player_data.objects.bulk_create(player_data_objs)
            Player.objects.bulk_create(player_objs)

            # Reload to get assigned PKs; compute disambiguated first_name, then
            # rand_id prefix on Player.full_name, then a single bulk_update.
            players_ = list(Player.objects.filter(tenant=tenant))
            player_datas = list(Player_data.objects.filter(tenant=tenant))
            for players in (players_, player_datas):
                firstNames = [unidecode(p.full_name) for p in players]
                for player in players:
                    value = player.full_name.split(" ")[0]
                    firstname = value
                    if value == "Player":
                        break
                    for _ in range(10):
                        ud_value = unidecode(value)
                        if sum(p[:len(ud_value)] == ud_value for p in firstNames) > 1:
                            value += player.full_name[len(value):len(value) + 1]
                        else:
                            break
                    value = value.rstrip()
                    player.first_name = value + "." if value != firstname else value
            for player in players_:
                player.full_name = f"{player.full_name}"
            Player.objects.bulk_update(players_, ['first_name', 'full_name'])
            Player_data.objects.bulk_update(player_datas, ['first_name'])

            Hand.objects.filter(tenant=tenant).delete()
            Position.objects.filter(tenant=tenant).delete()
            # The new schedule starts with empty scores, so any rounds that were
            # published for the previous tournament are now stale — unpublish them all.
            PublishedRound.objects.filter(tenant=tenant).delete()
            nb_players = len(players_)
            nb_tables = nb_players // 4
            players_by_rand = {p.rand_id: p for p in players_}
            pos_sheet = wb['{0} players'.format(nb_players)]
            # Materialize the full sheet once (rows 3..3+nb_rounds-1, cols 2..2+5*nb_tables-1).
            pos_rows = list(pos_sheet.iter_rows(
                min_row=3, max_row=2 + variables.nb_rounds,
                min_col=2, max_col=1 + 5 * nb_tables,
                values_only=True,
            ))
            positions_to_create = []
            for round_idx, row in enumerate(pos_rows):
                for table_nb in range(nb_tables):
                    for position in range(4):
                        player_rand_id = row[position + 5 * table_nb]
                        positions_to_create.append(Position(
                            tenant=tenant,
                            player=players_by_rand.get(player_rand_id),
                            round_nb=round_idx + 1,
                            table_nb=table_nb + 1,
                            position=position + 1,
                            minipoints=None,
                            tablepoints=None,
                        ))
            Position.objects.bulk_create(positions_to_create, batch_size=500)
            wb.close()
            from ..signals import invalidate_leaderboard
            invalidate_leaderboard(tenant.subdomain)
            broadcast_publish_state(tenant.subdomain, {'published_rounds': []})
        except Exception:
            Player.objects.filter(tenant=tenant).delete()
            Player_data.objects.filter(tenant=tenant).delete()
            Hand.objects.filter(tenant=tenant).delete()
            Position.objects.filter(tenant=tenant).delete()
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
def randomize(request):
    tenant = get_tenant(request)
    players = Player.objects.filter(tenant=tenant).order_by('rand_id')
    players_data = Player_data.objects.filter(tenant=tenant).order_by('full_name')
    for player in players:
        val = request.POST.get('player_' + str(player.id))
        if val and val not in ('no', ''):
            if val == "clear":
                player.full_name = "Player #" + str(player.rand_id)
                player.first_name = "#" + str(player.rand_id)
                player.EMA_ID = ""
                player.country = ""
                player.email = ""
                player.team = ""
            else:
                try:
                    player_data = [p for p in players_data if p.id == int(val)][0]
                except IndexError:
                    continue
                player.full_name = player_data.full_name
                player.first_name = player_data.first_name
                player.EMA_ID = player_data.EMA_ID
                player.country = player_data.country
                player.email = player_data.email
                player.team = player_data.team
            player.save()

    position_vals = Position.objects.filter(tenant=tenant).order_by('id')

    round_max = 0
    table_max = 0
    for position_val in position_vals:
        round_max = max(round_max, position_val.round_nb)
        table_max = max(table_max, position_val.table_nb)
    positions = []
    for _ in range(table_max):
        positions.append([])
        for _ in range(round_max):
            positions[-1].append([None, None, None, None])
    for position_val in position_vals:
        positions[position_val.table_nb - 1][position_val.round_nb - 1][position_val.position - 1] = position_val.player

    remaining_players = [p.full_name for p in players_data if p.full_name not in [p1.full_name for p1 in players]]
    template = loader.get_template('mahj/admin_randomize.html')
    context = {
        "positions": positions,
        "players_data": players_data,
        "players": players,
        "remaining_players": remaining_players,
    }
    return template.render(context, request)


@user_passes_test(lambda u: u.is_staff)
def admin_team_draw(request):
    tenant = get_tenant(request)
    players_data = Player_data.objects.filter(tenant=tenant).order_by('full_name')

    teams_dict = {}
    for pd in players_data:
        if pd.team:
            if pd.team not in teams_dict:
                teams_dict[pd.team] = []
            teams_dict[pd.team].append({
                "id": pd.id,
                "full_name": pd.full_name,
                "first_name": pd.first_name,
                "country": pd.country,
                "flag": _country_flag(pd.country),
                "EMA_ID": pd.EMA_ID,
            })

    teams_list = [
        {"name": name, "players": players}
        for name, players in sorted(teams_dict.items())
    ]

    nb_teams = len(teams_list)

    saved_draw = []
    players_with_team = Player.objects.filter(tenant=tenant).exclude(team="").order_by('rand_id')
    if players_with_team.exists():
        draw_teams = {}
        for p in players_with_team:
            if p.team not in draw_teams:
                draw_teams[p.team] = []
            draw_teams[p.team].append({
                "full_name": p.full_name,
                "rand_id": p.rand_id,
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
    assignments = data.get('assignments', [])

    pd_ids = [a['player_data_id'] for a in assignments]
    player_datas = {
        pd.id: pd
        for pd in Player_data.objects.filter(tenant=tenant, id__in=pd_ids)
    }

    players_by_rand = {
        p.rand_id: p
        for p in Player.objects.filter(tenant=tenant)
    }

    updated = []
    for a in assignments:
        pd = player_datas.get(a['player_data_id'])
        player = players_by_rand.get(a['rand_id'])
        if pd and player:
            player.full_name = pd.full_name
            player.first_name = pd.first_name
            player.EMA_ID = pd.EMA_ID
            player.country = pd.country
            player.email = pd.email
            player.team = pd.team
            updated.append(player)

    Player.objects.bulk_update(updated, ['full_name', 'first_name', 'EMA_ID', 'country', 'email', 'team'])

    return HttpResponse('OK')


@user_passes_test(lambda u: u.is_staff)
def update_variables(request):
    variables = get_variables(request)
    for name in ["welcome", "title", "fullname"]:
        value = request.POST.get(name, default=None)
        if value is not None:
            if "sw_class" in name:
                if value == "":
                    player = None
                else:
                    player = Player.objects.get(id=value)
                setattr(variables, name, player)
            else:
                setattr(variables, name, value)
    variables.save()
    return HttpResponse(str(variables))


def update_welcome(request):
    try:
        variables = get_variables(request)
        if request.GET.get('welcome', default=None) is not None:
            variables.welcome = request.GET.get('welcome')
            variables.save()
        welcome = variables.welcome
    except Exception:
        welcome = "Welcome"
    return HttpResponse(welcome)


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


def welcome_options(request):
    variables = get_variables(request)
    try:
        welcome = variables.welcome
    except Exception:
        welcome = "Welcome"
    template = loader.get_template('mahj/welcome_options.html')
    return HttpResponse(template.render({"welcome": welcome, "variables": variables}, request))


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
        template2 = loader.get_template('mahj/admin_welcome.html')
        page_content = template2.render({"error": error}, request)
    elif page == "display":
        variables = get_variables(request)
        if request.GET.get('action') == "set_variable":
            touched = False
            for var in request.GET.keys():
                if "variables-" in var:
                    field = var.replace("variables-", "")
                    if hasattr(variables, field):
                        setattr(variables, field, request.GET.get(var))
                        touched = True
            if touched:
                variables.save()
            return HttpResponse(str(variables))
        elif request.GET.get('action') == "add_screen":
            Screen(tenant=tenant, name="Screen_X", view="black").save()
            broadcast_display(tenant.subdomain, 'screen.update', {'event': 'screen_update'})
            return HttpResponseRedirect('admin?page=display')
        elif request.GET.get('action') == "remove_screen":
            Screen.objects.filter(tenant=tenant).last().delete()
            broadcast_display(tenant.subdomain, 'screen.update', {'event': 'screen_update'})
            return HttpResponseRedirect('admin?page=display')
        elif request.GET.get('action') == "add_mode":
            mode_name = request.POST.get('mode_name')
            screens = Screen.objects.filter(tenant=tenant).order_by('id')
            views_list = [str(screen.view) for screen in screens]
            ScreenMode(tenant=tenant, name=mode_name, views=json.dumps(views_list)).save()
            return HttpResponseRedirect('admin?page=display')
        elif request.GET.get('rm_mode'):
            mode = ScreenMode.objects.get(tenant=tenant, id=request.GET.get('rm_mode'))
            mode.delete()
            return HttpResponseRedirect('admin?page=display')
        elif request.GET.get('set_mode'):
            mode = ScreenMode.objects.get(tenant=tenant, id=request.GET.get('set_mode'))
            views_list = json.loads(mode.views)
            screens = Screen.objects.filter(tenant=tenant).order_by('id')
            applied = []
            for view, screen in zip(views_list, screens):
                screen.view = view
                screen.save()
                applied.append({'id': screen.id, 'view': screen.view})
            broadcast_display(tenant.subdomain, 'screen.update', {'event': 'screen_update'})
            # The admin page applies modes via AJAX so it can refresh the
            # selects/previews in place; other callers (mobile app) get a redirect.
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'screens': applied})
            return HttpResponseRedirect('admin?page=display')
        screens = Screen.objects.filter(tenant=tenant).order_by('id')
        modes = ScreenMode.objects.filter(tenant=tenant).order_by('id')
        context = {
            "screens": screens,
            "modes": modes,
            "screen_displays": ["black", "scores p. 1", "scores p. 2", "scores all", "scores all, total only", "counter", "schedule"],
            "variables": variables,
        }
        template2 = loader.get_template('mahj/admin_display.html')
        page_content = template2.render(context, request)
    elif page == "import_template":
        template2 = loader.get_template('mahj/admin_import_template.html')
        page_content = template2.render({}, request)
    elif page == "randomize":
        page_content = randomize(request)
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
            for rn, tn in Hand.objects.filter(tenant=tenant, hand_nb=17, pts=1)
                                       .values_list('round_nb', 'table_nb')
        }
        filled_keys = {
            f"{rn}-{tn}"
            for rn, tn in Hand.objects.filter(tenant=tenant, pts__gt=0)
                                       .exclude(hand_nb=17)
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
        }, request)
    else:
        page_content = "None"

    context = {
        "username": request.user.username,
        "page": page,
        "page_content": page_content,
        "user_is_scorer": is_scorer(request.user),
        "user_is_display_op": is_display_op(request.user),
        "user_is_publisher": is_publisher(request.user),
    }
    return HttpResponse(template.render(context, request))
