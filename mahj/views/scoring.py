from django.core.cache import cache

from .. import scoring as _scoring
from ..models import Player
from .helpers import get_tenant, get_variables


LEADERBOARD_TTL = 20   # rendered HTML: how long the page can be served stale.
SUB_CACHE_TTL = 300    # underlying data pieces: signals invalidate them on real writes,
                       # so this longer TTL is just a defensive safety net and lets the
                       # 20s HTML rebuild stay cheap (it just composes warm sub-caches).


def scores_per_table_json(request):
    return _scoring.scores_per_table(get_tenant(request), get_variables(request))


def scores_per_player_json(request, check_final=True, force_all=False, positions=None):
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    if positions is not None:
        return _scoring.player_standings(
            tenant, get_variables(request),
            check_final=check_final, force_all=force_all, positions=positions,
        )
    cache_key = f'leaderboard:{subdomain}:{check_final}:{force_all}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    scores = _scoring.player_standings(
        tenant, get_variables(request),
        check_final=check_final, force_all=force_all,
    )
    cache.set(cache_key, scores, SUB_CACHE_TTL)
    return scores


def player_rounds_json(request, player_id):
    tenant = get_tenant(request)
    return _scoring.player_rounds(tenant, Player.objects.get(id=player_id))


def stat_rounds(request, check_final=False, positions=None, hands=None):
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    if positions is not None or hands is not None:
        return _scoring.round_winners(
            tenant, get_variables(request), check_final,
            positions=positions, hands=hands,
        )
    cache_key = f'stat_rounds:{subdomain}:{check_final}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    result = _scoring.round_winners(tenant, get_variables(request), check_final)
    cache.set(cache_key, result, SUB_CACHE_TTL)
    return result


def stat_all_rounds(request, check_final=False, positions=None, hands=None):
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    if positions is not None or hands is not None:
        return _scoring.overall_winners(
            tenant, get_variables(request), check_final,
            positions=positions, hands=hands,
        )
    cache_key = f'stat_all:{subdomain}:{check_final}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    result = _scoring.overall_winners(tenant, get_variables(request), check_final)
    cache.set(cache_key, result, SUB_CACHE_TTL)
    return result


def table_stats(request, check_final=False, positions=None, hands=None):
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    if positions is not None or hands is not None:
        return _scoring.table_stats(
            tenant, get_variables(request), check_final,
            positions=positions, hands=hands,
        )
    cache_key = f'table_stats:{subdomain}:{check_final}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    result = _scoring.table_stats(tenant, get_variables(request), check_final)
    cache.set(cache_key, result, SUB_CACHE_TTL)
    return result


def tournament_seating(request, check_final=True, force_all=False, valid_pairs=None, positions=None):
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    if positions is not None:
        return _scoring.tournament_seating(
            tenant, get_variables(request),
            check_final=check_final, force_all=force_all,
            valid_pairs=valid_pairs, positions=positions,
        )
    cache_key = f'seating_v2:{subdomain}:{check_final}:{force_all}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    result = _scoring.tournament_seating(
        tenant, get_variables(request),
        check_final=check_final, force_all=force_all, valid_pairs=valid_pairs,
    )
    cache.set(cache_key, result, SUB_CACHE_TTL)
    return result
