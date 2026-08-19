from django.core.cache import cache

from .. import scoring as _scoring
from ..models import Player
from .helpers import get_tenant, get_tournament


LEADERBOARD_TTL = 20   # rendered HTML: how long the page can be served stale.
SUB_CACHE_TTL = 300    # the data pieces below.

# The invalidation contract, stated because it is easy to get wrong:
#
# Every cache below is keyed `<prefix>:<subdomain>:<full_view>`, and
# signals.invalidate_leaderboard() deletes those keys **by that exact prefix** for
# both full_view values on any real write. So the TTL is not how stale the data can
# get — a write clears it immediately — it is only a backstop for a write that
# somehow bypasses the signals. The prefixes are therefore fixed names shared with
# signals.py, never derived from the function, and adding a surface here means
# adding its prefix there too or it will serve stale data for up to SUB_CACHE_TTL.
#
# `full_view` is in the key because staff and the public may see a different number
# of rounds; a caller that passes its own `seats`/`hands` is not cached at all,
# since its result depends on what it passed rather than on the key.


def _cached(prefix, request, full_view, compute, bypass=False):
    """Cache-or-compute one scoring surface under the shared key scheme above.

    `bypass` is for callers that already hold the rows (a page assembling several
    surfaces from one query set): that result depends on what they passed, so it
    must never be written under the shared key.
    """
    if bypass:
        return compute()
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    cache_key = f'{prefix}:{subdomain}:{full_view}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    result = compute()
    cache.set(cache_key, result, SUB_CACHE_TTL)
    return result


def scores_per_table_grid(request):
    return _scoring.scores_per_table(get_tenant(request), get_tournament(request))


def scores_per_player_rows(request, full_view=False, seats=None):
    return _cached(
        'leaderboard', request, full_view,
        lambda: _scoring.player_standings(
            get_tenant(request), get_tournament(request),
            full_view=full_view, seats=seats),
        bypass=seats is not None)


def player_rounds_rows(request, player_id):
    tenant = get_tenant(request)
    # Scoped to the tenant: an unscoped id lookup would read another tenant's
    # competitor and render their rounds on this subdomain.
    return _scoring.player_rounds(
        tenant, Player.objects.get(tenant=tenant, id=player_id))


def stat_rounds(request, full_view=False, seats=None, hands=None):
    return _cached(
        'stat_rounds', request, full_view,
        lambda: _scoring.round_winners(
            get_tenant(request), get_tournament(request), full_view,
            seats=seats, hands=hands),
        bypass=seats is not None or hands is not None)


def stat_all_rounds(request, full_view=False, seats=None, hands=None):
    return _cached(
        'stat_all', request, full_view,
        lambda: _scoring.overall_winners(
            get_tenant(request), get_tournament(request), full_view,
            seats=seats, hands=hands),
        bypass=seats is not None or hands is not None)


def table_stats(request, full_view=False, seats=None, hands=None):
    return _cached(
        'table_stats', request, full_view,
        lambda: _scoring.table_stats(
            get_tenant(request), get_tournament(request), full_view,
            seats=seats, hands=hands),
        bypass=seats is not None or hands is not None)


def table_stats_rounds(request, full_view=False, seats=None, hands=None):
    return _cached(
        'table_stats_rounds', request, full_view,
        lambda: _scoring.table_stats_rounds(
            get_tenant(request), get_tournament(request), full_view,
            seats=seats, hands=hands),
        bypass=seats is not None or hands is not None)


def stats_export(request, full_view=False, seats=None, hands=None):
    return _scoring.stats_export(
        get_tenant(request), get_tournament(request), full_view,
        seats=seats, hands=hands,
    )


def tournament_seating(request, full_view=False, valid_pairs=None, seats=None):
    return _cached(
        'seating_v2', request, full_view,
        lambda: _scoring.tournament_seating(
            get_tenant(request), get_tournament(request),
            full_view=full_view, valid_pairs=valid_pairs, seats=seats),
        bypass=seats is not None)
