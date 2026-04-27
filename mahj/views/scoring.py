from django.core.cache import cache

from .. import scoring as _scoring
from ..models import Player
from .helpers import get_tenant, get_variables


LEADERBOARD_TTL = 20  # seconds; also invalidated on Position/Variable writes via signals.


def scores_per_table_json(request):
    return _scoring.scores_per_table(get_tenant(request), get_variables(request))


def scores_per_player_json(request, check_final=True, force_all=False):
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    cache_key = f'leaderboard:{subdomain}:{check_final}:{force_all}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    scores = _scoring.player_standings(
        tenant, get_variables(request),
        check_final=check_final, force_all=force_all,
    )
    cache.set(cache_key, scores, LEADERBOARD_TTL)
    return scores


def player_rounds_json(request, player_id):
    tenant = get_tenant(request)
    return _scoring.player_rounds(tenant, Player.objects.get(id=player_id))


def stat_rounds(request, check_final=False):
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    cache_key = f'stat_rounds:{subdomain}:{check_final}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    result = _scoring.round_winners(tenant, get_variables(request), check_final)
    cache.set(cache_key, result, LEADERBOARD_TTL)
    return result


def stat_all_rounds(request):
    return _scoring.overall_winners(get_tenant(request), get_variables(request))


def tournament_seating(request, check_final=True, force_all=False, valid_pairs=None):
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    cache_key = f'seating_v2:{subdomain}:{check_final}:{force_all}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    result = _scoring.tournament_seating(
        tenant, get_variables(request),
        check_final=check_final, force_all=force_all, valid_pairs=valid_pairs,
    )
    cache.set(cache_key, result, LEADERBOARD_TTL)
    return result
