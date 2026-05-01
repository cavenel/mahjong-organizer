"""Iframe-modal endpoints opened from the public desktop page."""
from urllib.request import urlopen

from bs4 import BeautifulSoup

from django.http import HttpResponse
from django.template import loader

from ..models import Hand, Player, Position
from .helpers import get_tenant, get_variables
from ..scoring import player_extra_stats, team_extra_stats
from .scoring import player_rounds_json, scores_per_player_json


def details_player(request, id):
    tenant = get_tenant(request)
    variables = get_variables(request)
    player = Player.objects.get(tenant=tenant, id=id)
    is_admin = request.user.is_staff
    scores_json = scores_per_player_json(request, check_final=True, force_all=is_admin)
    scores_json = [s for s in scores_json if s["player_id"] == id][0]
    rounds = player_rounds_json(request, id)
    extra_stats = player_extra_stats(tenant, player, variables)
    template = loader.get_template('mahj/modal_details_player.html')
    context = {
        'player': player,
        'player_rounds': rounds,
        'winds': ["East", "South", "West", "North"],
        'scores_json': scores_json,
        'rounds': range(1, 1 + len(scores_json["scores"])),
        'max_round': len(scores_json["scores"]),
        'variables': variables,
        'extra_stats': extra_stats,
    }
    return HttpResponse(template.render(context, request))


def details_player_ema(request, id):
    tenant = get_tenant(request)
    player = Player.objects.filter(tenant=tenant).get(id=id)
    url = "http://mahjong-europe.org/ranking/Players/{0}.html".format(player.EMA_ID)
    COL_COUNTRY, COL_RANK = 2, 5

    player_response = urlopen(url)
    player_parse = BeautifulSoup(player_response.read(), "lxml")
    player_country = player_parse.find(class_="contentpaneopen").table.find_all("img")[1]["src"]

    tournaments = player_parse.find_all(attrs={"rules": "GROUPS"})[0].find_all("tr")[1:]
    internal_tournaments, external_tournaments = [], []
    for tournament in tournaments:
        columns = tournament.find_all("td")
        tournament_country = columns[COL_COUNTRY].find_all("img")[0]["src"]
        rank = [float(r) for r in columns[COL_RANK].get_text().strip().split("/")]
        tournament_val = max(0, 1000 - (2000 / rank[1]) * (rank[0] - 1))
        if tournament_country == player_country:
            internal_tournaments.append(tournament_val)
        else:
            external_tournaments.append(tournament_val)
    val = sum(sorted(external_tournaments, reverse=True)[:2]) + sum(internal_tournaments)
    return HttpResponse(val)


def details_team(request, team_name):
    tenant = get_tenant(request)
    variables = get_variables(request)
    extra_stats = team_extra_stats(tenant, team_name, variables)
    if extra_stats is None:
        from django.http import Http404
        raise Http404

    is_admin = request.user.is_staff
    leaderboard = scores_per_player_json(request, check_final=True, force_all=is_admin)
    member_ids = {p['id'] for p in extra_stats['players']}
    members = sorted(
        [s for s in leaderboard if s['player_id'] in member_ids],
        key=lambda s: s['pos'],
    )

    # Team totals, rank, and per-round rank history.
    # max_played = highest round index for which any team member has a real score.
    max_played = max(
        (sum(1 for sc in s['scores'] if sc.get('tp') is not None) for s in members),
        default=0,
    )
    is_mcr = variables.rules == 'MCR'
    team_history_pos = []
    for rnd in range(1, max_played + 1):
        cumulative = {}
        for s in leaderboard:
            t = s.get('team') or ''
            if not t:
                continue
            slot = cumulative.setdefault(t, {'tp': 0.0, 'mp': 0})
            for sc in s['scores'][:rnd]:
                if sc.get('tp') is not None:
                    slot['tp'] += sc['tp']
                    slot['mp'] += sc.get('mp') or 0
        ranked = sorted(cumulative.items(), key=lambda x: (-x[1]['tp'] if is_mcr else 0, -x[1]['mp']))
        team_history_pos.append(
            next((i + 1 for i, (name, _) in enumerate(ranked) if name == team_name), None)
        )

    by_team = {}
    for s in leaderboard:
        t = s.get('team') or ''
        if not t:
            continue
        slot = by_team.setdefault(t, {'tp': 0.0, 'mp': 0})
        slot['tp'] += s['total'].get('tp') or 0
        slot['mp'] += s['total'].get('mp') or 0
    sort_key = (lambda x: -x[1]['tp']) if is_mcr else (lambda x: -x[1]['mp'])
    ranked_teams = sorted(by_team.items(), key=sort_key)
    team_pos = next((i + 1 for i, (name, _) in enumerate(ranked_teams) if name == team_name), None)
    team_total = by_team.get(team_name, {'tp': 0.0, 'mp': 0})

    template = loader.get_template('mahj/modal_details_team.html')
    context = {
        'team_name': team_name,
        'extra_stats': extra_stats,
        'members': members,
        'team_pos': team_pos,
        'team_total': team_total,
        'team_history_pos': team_history_pos,
        'variables': variables,
    }
    return HttpResponse(template.render(context, request))


def detailed_scores(request, round_nb, table_nb):
    tenant = get_tenant(request)
    position_vals = Position.objects.filter(tenant=tenant).order_by('id').filter(round_nb=round_nb, table_nb=table_nb)
    hand_vals = Hand.objects.filter(tenant=tenant).order_by('id').filter(round_nb=round_nb, table_nb=table_nb)

    all_hands = [None for _ in range(17)]
    for hand_val in hand_vals:
        all_hands[hand_val.hand_nb - 1] = hand_val

    if all_hands[16] is None:
        h = Hand(tenant=tenant, round_nb=round_nb, table_nb=table_nb, hand_nb=17, pts=0, win_by=0, win_from=0)
        h.save()

    hands_per_wind = []
    for i, wind in enumerate(["East", "South", "West", "North"]):
        hands_per_wind.append([wind, []])
        for j in range(4):
            h = all_hands[i * 4 + j]
            if h is None:
                h = Hand(tenant=tenant, round_nb=round_nb, table_nb=table_nb, hand_nb=i * 4 + j + 1, pts=0, win_by=0, win_from=0)
                h.save()
            hands_per_wind[-1][1].append(h)

    scores = [None, None, None, None]
    for position_val in position_vals:
        scores[position_val.position - 1] = position_val

    template = loader.get_template('mahj/modal_detailed_scores.html')
    context = {
        'hands_per_wind': hands_per_wind,
        'completed': all_hands[16],
        'scores': scores,
        'round_nb': round_nb,
        'table_nb': table_nb,
    }
    return HttpResponse(template.render(context, request))
