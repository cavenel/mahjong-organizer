"""Iframe-modal endpoints opened from the public desktop page."""
import hashlib

from django.core.cache import cache
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.template import loader

from ..models import Hand, Player, Seat
from .helpers import get_tenant, get_tournament, is_tenant_admin
from ..scoring import (
    _attach_players, player_extra_stats, public_round_max, rounds_played,
    team_extra_stats, team_standings,
)
from ..signals import leaderboard_gen
from .scoring import player_rounds_rows, scores_per_player_rows


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

    tournament = get_tournament(request)
    # Public and enumerable, so an unknown or other-tenant id is a 404 — the team
    # modal already answers that way. It used to raise DoesNotExist (a 500).
    player = get_object_or_404(Player, tenant=tenant, id=id)
    standings = scores_per_player_rows(request, full_view=is_admin)
    standings_row = next((s for s in standings if s["player_id"] == id), None)
    if standings_row is None:
        # player_standings covers every player of the tenant, so this means the
        # cached standings predate this player. Nothing to render a modal from.
        raise Http404('no standings row for this competitor yet')
    rounds = player_rounds_rows(request, id)
    # Cap the placement/hand cards to the same rounds the score grid shows, so a
    # withheld final round can't leak into them ahead of the ceremony.
    extra_stats = player_extra_stats(
        tenant, player, tournament,
        max_round=public_round_max(tenant, tournament, full_view=is_admin),
    )
    template = loader.get_template('mahj/modal_details_player.html')
    context = {
        'player': player,
        'player_rounds': rounds,
        'winds': ["East", "South", "West", "North"],
        'standings_row': standings_row,
        'rounds': range(1, 1 + len(standings_row["scores"])),
        'max_round': len(standings_row["scores"]),
        'tournament': tournament,
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

    tournament = get_tournament(request)
    extra_stats = team_extra_stats(
        tenant, team_name, tournament,
        max_round=public_round_max(tenant, tournament, full_view=is_admin),
    )
    if extra_stats is None:
        raise Http404

    leaderboard = scores_per_player_rows(request, full_view=is_admin)
    member_ids = {p['id'] for p in extra_stats['players']}
    members = sorted(
        [s for s in leaderboard if s['player_id'] in member_ids],
        key=lambda s: s['pos'],
    )

    # Per-round rank history, one entry per round the team has played, ranked by
    # the same `team_standings` the leaderboard's team table uses — so the chart's
    # last point is the position shown beside it whatever the rules.
    max_played = rounds_played(members)
    team_history_pos = [
        next((t['pos'] for t
              in team_standings([_row_through_round(s, rnd) for s in leaderboard],
                                tournament, rnd)
              if t['team'] == team_name), None)
        for rnd in range(1, max_played + 1)
    ]

    # Final team rank and totals: tied teams share a position, like the leaderboard.
    team_rows = team_standings(leaderboard, tournament, tournament.nb_rounds)
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
        # Rendered through |json_script in the template: a round where the team
        # isn't ranked yet is None, and Python's repr of that ("None") is a syntax
        # error in JS that killed the whole modal script.
        'team_history_pos': team_history_pos,
        'tournament': tournament,
    }
    html = template.render(context, request)
    cache.set(cache_key, html, MODAL_CACHE_TTL)
    return HttpResponse(html)


def _row_through_round(row, round_nb):
    """One standings row cut back to rounds 1..round_nb, in the shape
    ``team_standings`` reads — the per-round frame of the team rank chart.

    Selected on each score's own ``round_nb``, never on its position in the list.
    The lists are compact (one entry per round the competitor actually played), so
    taking the first N entries folds a later round's score into an earlier frame
    for anyone the chart doesn't seat every round — a bye, or a substitute given a
    fresh draw number mid-tournament. ``rounds_played`` reads round numbers for
    the same reason.

    ``tp`` totals a number even under rules that never fill table points in, so
    the row has the one shape every consumer expects (see ``_cumulative_row``).
    """
    scores = [sc for sc in row.get('scores') or () if sc['round_nb'] <= round_nb]
    return {
        'team': row.get('team') or '',
        'player_id': row['player_id'],
        'flag': row.get('flag') or '',
        'scores': scores,
        'total': {
            'tp': sum(0 if sc.get('tp') is None else sc['tp'] for sc in scores),
            'mp': sum(0 if sc.get('mp') is None else sc['mp'] for sc in scores),
        },
    }


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
    # Bounded to the seating chart before anything is cached: the key is built from
    # URL coordinates, so a crawler walking /detailed_scores_<r>_<t> could otherwise
    # fill the cache with one placeholder entry per pair it invented.
    if not Seat.objects.filter(tenant=tenant, round_nb=round_nb, table_nb=table_nb).exists():
        raise Http404('no such table in the seating chart')

    cache_key = f'modal_detailed:{subdomain}:{round_nb}:{table_nb}:{is_admin}:{leaderboard_gen(subdomain)}'
    cached = cache.get(cache_key)
    if cached is not None:
        return HttpResponse(cached)

    # This is the raw hand-by-hand grid, so it must honour the same reveal masking
    # as the standings: a public viewer may not open a round held back for the
    # ceremony (unpublished, or the final round in pre-publish suspense). Staff see
    # everything. Mirrors public_round_max used by the standings/modal stat cards.
    if not is_admin and round_nb > public_round_max(tenant, get_tournament(request)):
        template = loader.get_template('mahj/modal_not_revealed.html')
        html = template.render({'round_nb': round_nb, 'table_nb': table_nb}, request)
        cache.set(cache_key, html, MODAL_CACHE_TTL)
        return HttpResponse(html)

    seat_rows = _attach_players(tenant, list(
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
    for seat in seat_rows:
        if 1 <= seat.wind <= 4:
            scores[seat.wind - 1] = seat

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
