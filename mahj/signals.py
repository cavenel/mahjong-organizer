"""Cache invalidation and WebSocket broadcast for leaderboard and tournament variables.

Broadcast groups:
  leaderboard_{subdomain} — public displays: fired ONLY on round publish/unpublish.
  display_{subdomain}     — Variable saves, screen switches, counter writes.
  scorers_{subdomain}     — fine-grained row sync between scorer pages (no cache bust).
"""
import logging

from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from .models import Tenant, Variable

logger = logging.getLogger(__name__)


def leaderboard_gen(subdomain):
    """Monotonic counter, bumped on every leaderboard invalidation.

    Used as a cache-key component for per-id modal caches (details_player /
    details_team) so they bust on any real write without having to enumerate
    every player/team id — the old key is simply orphaned and expires via TTL.
    """
    return cache.get(f'leaderboard_gen:{subdomain}') or 0


def _bump_leaderboard_gen(subdomain):
    key = f'leaderboard_gen:{subdomain}'
    try:
        cache.incr(key)
    except ValueError:
        # Key absent (first write, or evicted). Seed it with no expiry so the
        # counter survives as long as Redis does.
        cache.set(key, 1, None)


def _invalidate_leaderboard(subdomain):
    _bump_leaderboard_gen(subdomain)
    for cf in (True, False):
        for fa in (True, False):
            cache.delete(f'leaderboard:{subdomain}:{cf}:{fa}')
    for cf in (True, False):
        cache.delete(f'stat_rounds:{subdomain}:{cf}')
    cache.delete(f'stat_all:{subdomain}')
    cache.delete(f'seating:{subdomain}')
    cache.delete(f'player_table:{subdomain}')
    for cf in (True, False):
        for fa in (True, False):
            cache.delete(f'seating_v2:{subdomain}:{cf}:{fa}')
    # One entry per desktop view token (see views/public.py: anonymous crowd +
    # the privileged role classes). All are busted on any leaderboard write.
    for view in ('staff', 'admin', 'user', 'anon'):
        cache.delete(f'desktop_html:{subdomain}:{view}')


def _broadcast(group, event_type, data):
    """Send a Channels group_send; best-effort.

    No-ops if the layer isn't configured, and swallows any send failure (e.g. a
    transient Redis blip): broadcasts run *after* the DB write in score-entry and
    publish paths, so a messaging error must never turn an already-committed write
    into a 500 for the scorer. Worst case a live page misses one update and resyncs
    on its next reconnect/refresh.
    """
    from channels.layers import get_channel_layer
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    try:
        async_to_sync(channel_layer.group_send)(
            group,
            {'type': event_type, 'data': data},
        )
    except Exception:
        logger.warning("Channels broadcast to %s failed", group, exc_info=True)


def broadcast_display(subdomain, event_type, data):
    """Push an event to all display consumers watching this tenant."""
    _broadcast(f'display_{subdomain}', event_type, data)


def broadcast_scorer_row(subdomain, data):
    """Send a row delta to scorer pages only (no cache bust, no public refresh)."""
    _broadcast(f'scorers_{subdomain}', 'scorer.row', data)


def broadcast_publish_state(subdomain, data):
    """Sync Publish-toggle state across scorer pages."""
    _broadcast(f'scorers_{subdomain}', 'publish.state', data)


def broadcast_scorer_validation(subdomain, data):
    """Notify scorer pages that a table's validation state changed."""
    _broadcast(f'scorers_{subdomain}', 'scorer.validation', data)


def broadcast_scorer_filled(subdomain, data):
    """Notify scorer pages that a table's filled state changed (has hand data or not)."""
    _broadcast(f'scorers_{subdomain}', 'scorer.filled', data)


def invalidate_leaderboard(subdomain):
    """Called from publish/unpublish paths: bust caches and wake public displays."""
    _invalidate_leaderboard(subdomain)
    _broadcast(f'leaderboard_{subdomain}', 'leaderboard.update', {'event': 'leaderboard_update'})


@receiver([post_save, post_delete], sender=Variable)
def on_variable_change(sender, instance, **kwargs):
    subdomain = instance.tenant.subdomain if instance.tenant_id else ''
    cache.delete(f'variables:{subdomain}')
    _invalidate_leaderboard(subdomain)
    broadcast_display(subdomain, 'variables.update', {'event': 'variables_update'})


@receiver([post_save, post_delete], sender=Tenant)
def on_tenant_change(sender, instance, **kwargs):
    cache.delete(f'tenant:{instance.subdomain}')


def connect_signals():
    """Called from apps.py — importing this module registers the @receiver handlers."""
