"""Low-level scoring helpers shared across the scoring package.

Depends only on models — never on the other scoring submodules — so it can be
imported anywhere in the package without cycles.
"""
from collections import defaultdict
from functools import lru_cache

import pycountry

from ..models import Player, ScoreSheet, Schedule


WINDS = ('East', 'South', 'West', 'North')


def _group_by(iterable, key):
    """Return a defaultdict(list) keyed by key(item). Missing keys return []."""
    out = defaultdict(list)
    for item in iterable:
        out[key(item)].append(item)
    return out


def _attach_players(tenant, seats):
    """Resolve each Seat's competitor from its draw_number and attach it as
    transient ``.player`` / ``.player_id`` attributes.

    The draw lives on ``Player.draw_number`` (a Seat links to a competitor only by
    draw number), so code that reads ``seat.player`` / ``seat.player_id`` gets it
    from here. One players query, then in-memory. Returns the same list.
    """
    players = {
        p.draw_number: p
        for p in Player.objects.filter(tenant=tenant, draw_number__isnull=False)
    }
    for s in seats:
        s.player = players.get(s.draw_number)
        s.player_id = s.player.id if s.player else None
    return seats


def player_schedule(tenant):
    """The round/session rows used by player_rounds, fetched once."""
    return [
        s for s in Schedule.objects.filter(tenant=tenant, is_round=True).order_by('id')
    ]


def completed_tables(tenant):
    """Set of (round_nb, table_nb) whose score sheet is validated."""
    return {
        (s.round_nb, s.table_nb)
        for s in ScoreSheet.objects.filter(tenant=tenant, validated=True)
    }


# Country names pycountry's exact + fuzzy lookup gets wrong or misses, mapped to
# their ISO alpha-2 flag code. "Turkey" misses entirely (pycountry renamed it
# "Türkiye"), so a Turkish player would get no flag and be excluded from "Best
# European". Bare "Korea" fuzzy-matches "Korea, Democratic People's Republic of"
# (kp, North Korea) instead of South Korea (kr). Keys are lower-cased and matched
# after stripping a leading "The ".
_FLAG_ALIASES = {
    'turkey': 'tr',
    'korea': 'kr',
    'south korea': 'kr',
    'chinese taipei': 'tw',
}


@lru_cache(maxsize=256)
def _country_flag(country):
    if country == "Independent":
        return 'mi'
    try:
        name = country.replace('The ', '').strip()
        if not name:
            return ''  # search_fuzzy('') matches an arbitrary country (gb); short-circuit
        alias = _FLAG_ALIASES.get(name.lower())
        if alias:
            return alias
        match = pycountry.countries.get(name=name)
        if match is None:
            results = pycountry.countries.search_fuzzy(name)
            match = results[0] if results else None
        return match.alpha_2.lower() if match else ''
    except Exception:
        return ''
