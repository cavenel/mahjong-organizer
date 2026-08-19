from django.core.cache import cache

from .. import scoring as _scoring
from ..models import Player
from .helpers import get_tenant, get_tournament


LEADERBOARD_TTL = 20   # rendered HTML: how long the page can be served stale.
SUB_CACHE_TTL = 300    # underlying data pieces: signals invalidate them on real writes,
                       # so this longer TTL is just a defensive safety net and lets the
                       # 20s HTML rebuild stay cheap (it just composes warm sub-caches).


def scores_per_table_grid(request):
    return _scoring.scores_per_table(get_tenant(request), get_tournament(request))


def scores_per_player_rows(request, full_view=False, seats=None):
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    if seats is not None:
        return _scoring.player_standings(
            tenant, get_tournament(request),
            full_view=full_view, seats=seats,
        )
    cache_key = f'leaderboard:{subdomain}:{full_view}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    scores = _scoring.player_standings(
        tenant, get_tournament(request),
        full_view=full_view,
    )
    cache.set(cache_key, scores, SUB_CACHE_TTL)
    return scores


def player_rounds_rows(request, player_id):
    tenant = get_tenant(request)
    # Scoped to the tenant: an unscoped id lookup would read another tenant's
    # competitor and render their rounds on this subdomain.
    return _scoring.player_rounds(
        tenant, Player.objects.get(tenant=tenant, id=player_id))


def stat_rounds(request, full_view=False, seats=None, hands=None):
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    if seats is not None or hands is not None:
        return _scoring.round_winners(
            tenant, get_tournament(request), full_view,
            seats=seats, hands=hands,
        )
    cache_key = f'stat_rounds:{subdomain}:{full_view}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    result = _scoring.round_winners(tenant, get_tournament(request), full_view)
    cache.set(cache_key, result, SUB_CACHE_TTL)
    return result


def stat_all_rounds(request, full_view=False, seats=None, hands=None):
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    if seats is not None or hands is not None:
        return _scoring.overall_winners(
            tenant, get_tournament(request), full_view,
            seats=seats, hands=hands,
        )
    cache_key = f'stat_all:{subdomain}:{full_view}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    result = _scoring.overall_winners(tenant, get_tournament(request), full_view)
    cache.set(cache_key, result, SUB_CACHE_TTL)
    return result


def table_stats(request, full_view=False, seats=None, hands=None):
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    if seats is not None or hands is not None:
        return _scoring.table_stats(
            tenant, get_tournament(request), full_view,
            seats=seats, hands=hands,
        )
    cache_key = f'table_stats:{subdomain}:{full_view}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    result = _scoring.table_stats(tenant, get_tournament(request), full_view)
    cache.set(cache_key, result, SUB_CACHE_TTL)
    return result


def table_stats_rounds(request, full_view=False, seats=None, hands=None):
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    if seats is not None or hands is not None:
        return _scoring.table_stats_rounds(
            tenant, get_tournament(request), full_view,
            seats=seats, hands=hands,
        )
    cache_key = f'table_stats_rounds:{subdomain}:{full_view}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    result = _scoring.table_stats_rounds(tenant, get_tournament(request), full_view)
    cache.set(cache_key, result, SUB_CACHE_TTL)
    return result


def stats_export(request, full_view=False, seats=None, hands=None):
    return _scoring.stats_export(
        get_tenant(request), get_tournament(request), full_view,
        seats=seats, hands=hands,
    )


def tournament_seating(request, full_view=False, valid_pairs=None, seats=None):
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    if seats is not None:
        return _scoring.tournament_seating(
            tenant, get_tournament(request),
            full_view=full_view,
            valid_pairs=valid_pairs, seats=seats,
        )
    cache_key = f'seating_v2:{subdomain}:{full_view}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    result = _scoring.tournament_seating(
        tenant, get_tournament(request),
        full_view=full_view, valid_pairs=valid_pairs,
    )
    cache.set(cache_key, result, SUB_CACHE_TTL)
    return result
