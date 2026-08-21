from django.core.cache import cache

from .. import scoring as _scoring
from ..models import Player
from .helpers import get_tenant, get_tournament


LEADERBOARD_TTL = 20   # the public leaderboard's staleness budget, in seconds.
                       # Set here beside SUB_CACHE_TTL to keep the two visible
                       # together; views/public.py is the only consumer and
                       # applies it to both its data and its rendered HTML.
SUB_CACHE_TTL = 300    # the data pieces below.

# The invalidation contract, stated exactly because it is easy to assume more than
# is true:
#
# Every cache below is keyed `<prefix>:<subdomain>:<full_view>`, and
# signals.invalidate_leaderboard() deletes those keys by that exact prefix, for both
# full_view values. But it is NOT a model signal — there is no post_save receiver on
# Seat or Hand. It is called explicitly from the paths that change what the standings
# should say: publish/unpublish, template import, tournament reset, the draw.
#
# Score entry does not call it, and cannot be caught by a signal either: it writes
# with .update() / bulk_update(), which fire none. That is deliberate — scorers see
# their own edits live through the scorer WebSocket row sync, not through these
# caches — but it means a standings read can lag live entry by up to SUB_CACHE_TTL,
# for an admin full_view as well as the public. That bound is the reason the TTL is
# five minutes rather than an hour.
#
# So: the prefixes are fixed names shared with signals.py, never derived from the
# function, and adding a surface here means adding its prefix there too or it will go
# stale for a whole publish cycle rather than SUB_CACHE_TTL.
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
