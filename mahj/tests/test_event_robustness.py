"""Regression tests for the event-day robustness fixes (docs/event-day-review.md).

E7 — per-player/per-team modals are cached and bust on a real leaderboard write.
E8 — display standings don't 500 when there are fewer than 12 players.
"""
import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, override_settings

from mahj import signals
from mahj.models import Player, Position
from mahj.views import details_player, details_team, scores_per_player

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


def _req(user=None):
    rf = RequestFactory()
    req = rf.get('/', HTTP_HOST='test.mahj.ovh')
    req.user = user or AnonymousUser()
    return req


@override_settings(CACHES=LOCMEM)
def test_player_modal_is_cached_then_busted_on_invalidate(tournament):
    """E7: details_player is cached, and a real write busts it via the gen key."""
    from django.core.cache import cache
    cache.clear()
    pid = tournament['players'][0].id

    first = details_player(_req(), pid)
    assert first.status_code == 200

    # Served from cache (same generation).
    assert cache.get(
        f'modal_player:test:{pid}:False:{signals.leaderboard_gen("test")}'
    ) is not None

    # A real write bumps the generation, orphaning the old key.
    gen_before = signals.leaderboard_gen('test')
    signals.invalidate_leaderboard('test')
    assert signals.leaderboard_gen('test') == gen_before + 1
    assert cache.get(
        f'modal_player:test:{pid}:False:{signals.leaderboard_gen("test")}'
    ) is None  # live key is now a miss → fresh render on next open.


@override_settings(CACHES=LOCMEM)
def test_team_modal_is_cached(tournament):
    """E7: details_team is cached under a hashed team-name key."""
    import hashlib
    from django.core.cache import cache
    cache.clear()
    for i, p in enumerate(tournament['players']):
        p.team = f'Team{i % 4}'
        p.save()

    resp = details_team(_req(), 'Team0')
    assert resp.status_code == 200
    h = hashlib.md5('Team0'.encode('utf-8')).hexdigest()
    assert cache.get(
        f'modal_team:test:{h}:False:{signals.leaderboard_gen("test")}'
    ) is not None


def test_scores_per_player_page_with_under_12_players(tournament):
    """E8: standings render with < 12 players instead of raising IndexError."""
    # Trim to 10 players (and their positions) so scores_json[11] is out of range.
    keep = {p.id for p in tournament['players'][:10]}
    Position.objects.filter(tenant=tournament['tenant']).exclude(
        player_id__in=keep
    ).delete()
    Player.objects.filter(tenant=tournament['tenant']).exclude(
        id__in=keep
    ).delete()

    resp = scores_per_player(_req(), 'html', 1)  # page_nb=1 hits the guard
    assert resp.status_code == 200
