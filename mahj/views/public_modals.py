"""Iframe-modal endpoints opened from the public desktop page."""
import hashlib

from django.core.cache import cache
from django.http import HttpResponse
from django.template import loader

from ..models import Hand, Player, Seat
from .helpers import get_tenant, get_variables, is_tenant_admin
from ..scoring import (
    _assign_ranks, _attach_players, _standings_rank_key, _standings_sort_key,
    player_extra_stats, public_round_max, team_extra_stats, team_standings,
)
from ..signals import leaderboard_gen
from .scoring import player_rounds_json, scores_per_player_json


# These modals run uncached, heavy per-open queries (especially details_team,
# which rebuilds the full team rank history per round) and hundreds of players
# hammer them during the event. Cache the rendered HTML briefly, keyed by the
# leaderboard generation so a real write (score edit / publish) busts it.
MODAL_CACHE_TTL = 30


def details_player(request, id):
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    is_admin = is_tenant_admin(request)
    cache_key = f'modal_player:{subdomain}:{id}:{is_admin}:{leaderboard_gen(subdomain)}'
    cached = cache.get(cache_key)
    if cached is not None:
        return HttpResponse(cached)

    variables = get_variables(request)
    player = Player.objects.get(tenant=tenant, id=id)
    scores_json = scores_per_player_json(request, check_final=True, force_all=is_admin)
    scores_json = [s for s in scores_json if s["player_id"] == id][0]
    rounds = player_rounds_json(request, id)
    # Cap the placement/hand cards to the same rounds the score grid shows, so a
    # withheld final round can't leak into them ahead of the ceremony.
    extra_stats = player_extra_stats(
        tenant, player, variables,
        max_round=public_round_max(tenant, variables, force_all=is_admin),
    )
    template = loader.get_template('mahj/modal_details_player.html')
    context = {
        'player': player,
        'player_rounds': rounds,
        'winds': ["East", "South", "West", "North"],
        'scores_json': scores_json,
        'rounds': range(1, 1 + len(scores_json["scores"])),
        'max_round': len(scores_json["scores"]),
        'tournament': variables,
        'extra_stats': extra_stats,
    }
    html = template.render(context, request)
    cache.set(cache_key, html, MODAL_CACHE_TTL)
    return HttpResponse(html)


def details_team(request, team_name):
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    is_admin = is_tenant_admin(request)
    # Hash team_name into the key: it's free-form (spaces/unicode) and Django's
    # cache key validation rejects those.
    team_hash = hashlib.md5(team_name.encode('utf-8')).hexdigest()
    cache_key = f'modal_team:{subdomain}:{team_hash}:{is_admin}:{leaderboard_gen(subdomain)}'
    cached = cache.get(cache_key)
    if cached is not None:
        return HttpResponse(cached)

    variables = get_variables(request)
    extra_stats = team_extra_stats(
        tenant, team_name, variables,
        max_round=public_round_max(tenant, variables, force_all=is_admin),
    )
    if extra_stats is None:
        from django.http import Http404
        raise Http404

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
    sort_key = _standings_sort_key(variables)
    rank_key = _standings_rank_key(variables)
    team_history_pos = []
    for rnd in range(1, max_played + 1):
        cumulative = {}
        for s in leaderboard:
            t = s.get('team') or ''
            if not t:
                continue
            slot = cumulative.setdefault(t, {'team': t, 'total': {'tp': 0.0, 'mp': 0}})
            for sc in s['scores'][:rnd]:
                if sc.get('tp') is not None:
                    slot['total']['tp'] += sc['tp']
                    slot['total']['mp'] += sc.get('mp') or 0
        ranked = sorted(cumulative.values(), key=sort_key)
        _assign_ranks(ranked, rank_key, field='pos')
        team_history_pos.append(
            next((r['pos'] for r in ranked if r['team'] == team_name), None)
        )

    # Final team rank and totals: tied teams share a position, like the leaderboard.
    team_rows = team_standings(leaderboard, variables, variables.nb_rounds)
    match = next((t for t in team_rows if t['team'] == team_name), None)
    team_pos = match['pos'] if match else None
    team_total = match['total'] if match else {'tp': 0.0, 'mp': 0}

    template = loader.get_template('mahj/modal_details_team.html')
    context = {
        'team_name': team_name,
        'extra_stats': extra_stats,
        'members': members,
        'team_pos': team_pos,
        'team_total': team_total,
        'team_history_pos': team_history_pos,
        'tournament': variables,
    }
    html = template.render(context, request)
    cache.set(cache_key, html, MODAL_CACHE_TTL)
    return HttpResponse(html)


def detailed_scores(request, round_nb, table_nb):
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    is_admin = is_tenant_admin(request)

    # Public modal hit by the whole crowd (every table cell on the desktop links
    # here, including unplayed rounds). Cache the rendered HTML briefly, keyed by
    # the leaderboard generation so a real write (including publish/reveal) busts
    # it; the 30s TTL bounds staleness for hand edits (which don't bump the
    # generation). Keyed on is_admin too, since staff and public can see different
    # rounds — and the not-yet-revealed placeholder below is cached the same way.
    cache_key = f'modal_detailed:{subdomain}:{round_nb}:{table_nb}:{is_admin}:{leaderboard_gen(subdomain)}'
    cached = cache.get(cache_key)
    if cached is not None:
        return HttpResponse(cached)

    # This is the raw hand-by-hand grid, so it must honour the same reveal masking
    # as the standings: a public viewer may not open a round held back for the
    # ceremony (unpublished, or the final round in pre-publish suspense). Staff see
    # everything. Mirrors public_round_max used by the standings/modal stat cards.
    if not is_admin and round_nb > public_round_max(tenant, get_variables(request)):
        template = loader.get_template('mahj/modal_not_revealed.html')
        html = template.render({'round_nb': round_nb, 'table_nb': table_nb}, request)
        cache.set(cache_key, html, MODAL_CACHE_TTL)
        return HttpResponse(html)

    position_vals = _attach_players(tenant, list(
        Seat.objects.filter(tenant=tenant, round_nb=round_nb, table_nb=table_nb).order_by('id')
    ))
    hand_vals = Hand.objects.filter(tenant=tenant, round_nb=round_nb, table_nb=table_nb).order_by('id')

    all_hands = [None for _ in range(16)]
    for hand_val in hand_vals:
        if 1 <= hand_val.hand_nb <= 16:
            all_hands[hand_val.hand_nb - 1] = hand_val

    # Build the 16-slot grid in memory. Missing slots get an UNSAVED placeholder:
    # this is a read path, so a spectator opening an unplayed table must not write
    # rows to the DB. Score entry (admin_scores_per_hand) owns row creation.
    hands_per_wind = []
    for i, wind in enumerate(["East", "South", "West", "North"]):
        hands_per_wind.append([wind, []])
        for j in range(4):
            h = all_hands[i * 4 + j]
            if h is None:
                h = Hand(tenant=tenant, round_nb=round_nb, table_nb=table_nb,
                         hand_nb=i * 4 + j + 1, points=0, win_by=None, win_from=None)
            hands_per_wind[-1][1].append(h)

    scores = [None, None, None, None]
    for position_val in position_vals:
        if 1 <= position_val.wind <= 4:
            scores[position_val.wind - 1] = position_val

    # Per-player penalties only surface here when at least one is non-zero, so a
    # clean table stays clean.
    penalties = [s.penalty if s else 0 for s in scores]

    template = loader.get_template('mahj/modal_detailed_scores.html')
    html = template.render({
        'hands_per_wind': hands_per_wind,
        'scores': scores,
        'penalties': penalties,
        'show_penalties': any(penalties),
        'round_nb': round_nb,
        'table_nb': table_nb,
    }, request)
    cache.set(cache_key, html, MODAL_CACHE_TTL)
    return HttpResponse(html)
