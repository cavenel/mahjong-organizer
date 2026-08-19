from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .models import Membership, Tenant


def scorer_socket_allowed(user, subdomain):
    """Authorization policy for the scorers WebSocket: a scorer, publisher or
    tenant admin of ``subdomain``'s tenant may join (platform superusers bypass);
    everyone else — anonymous, or a member of a different tenant — is refused.
    Mirrors the HTTP scoring endpoints' ``tenant_role_required``.

    Kept as a plain synchronous function (not inlined in the consumer) so it can
    be unit-tested directly, without the async socket machinery; the consumer
    wraps it in ``database_sync_to_async``. The two no-DB short-circuits
    (anonymous, superuser) also let integration tests exercise the real connect
    path without a cross-thread ORM hop."""
    if user is None or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    tenant = Tenant.objects.filter(subdomain=subdomain).first()
    if tenant is None:
        return False
    m = Membership.objects.filter(user=user, tenant=tenant).first()
    return bool(m and (m.is_tenant_admin or m.is_scorer or m.is_publisher))


class TenantConsumer(AsyncJsonWebsocketConsumer):
    """Consumer for public displays (desktop, screen).

    Joins:
      leaderboard_{subdomain} — fired on round publish/unpublish only.
      display_{subdomain}     — screen switches, tournament changes, counter updates.
    """

    async def connect(self):
        subdomain = self.scope['url_route']['kwargs']['subdomain']
        self.leaderboard_group = f'leaderboard_{subdomain}'
        self.display_group = f'display_{subdomain}'
        await self.channel_layer.group_add(self.leaderboard_group, self.channel_name)
        await self.channel_layer.group_add(self.display_group, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.leaderboard_group, self.channel_name)
        await self.channel_layer.group_discard(self.display_group, self.channel_name)

    async def receive_json(self, content, **kwargs):
        # Clients send {"type":"ping"} as a keepalive/half-open probe; answer so
        # their pong watchdog can tell a live socket from a dead one. Anything
        # else is ignored (the base class would otherwise raise on receipt).
        if content.get('type') == 'ping':
            await self.send_json({'event': 'pong'})

    # --- handlers (type field dot → underscore) ---

    async def leaderboard_update(self, event):
        await self.send_json(event['data'])

    async def screen_update(self, event):
        await self.send_json(event['data'])

    async def tournament_update(self, event):
        await self.send_json(event['data'])

    async def counter_update(self, event):
        await self.send_json(event['data'])

    async def ceremony_update(self, event):
        await self.send_json(event['data'])


class ScorersConsumer(AsyncJsonWebsocketConsumer):
    """Consumer for scorer pages: live row sync and publish-toggle sync.

    Unlike the public ``TenantConsumer``, this stream carries *unpublished* live
    scores (per-seat minipoints/tablepoints as they are typed, plus publish and
    validation state), so it is gated exactly like the HTTP scoring endpoints:
    only a scorer, publisher or tenant admin of this subdomain's tenant may join
    (platform superusers bypass). Anyone else — anonymous, or a member of a
    different tenant — is refused the socket, which is what keeps the withheld
    final from leaking to spectators.
    """

    async def connect(self):
        subdomain = self.scope['url_route']['kwargs']['subdomain']
        allowed = await database_sync_to_async(scorer_socket_allowed)(
            self.scope.get('user'), subdomain)
        if not allowed:
            await self.close()
            return
        self.group = f'scorers_{subdomain}'
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        # connect() only sets self.group after authorizing; a refused socket has
        # no group to leave.
        group = getattr(self, 'group', None)
        if group is not None:
            await self.channel_layer.group_discard(group, self.channel_name)

    async def receive_json(self, content, **kwargs):
        # Clients send {"type":"ping"} as a keepalive/half-open probe; answer so
        # their pong watchdog can tell a live socket from a dead one. Anything
        # else is ignored (the base class would otherwise raise on receipt).
        if content.get('type') == 'ping':
            await self.send_json({'event': 'pong'})

    async def scorer_row(self, event):
        await self.send_json(event['data'])

    async def publish_state(self, event):
        await self.send_json(event['data'])

    async def scorer_validation(self, event):
        await self.send_json(event['data'])

    async def scorer_filled(self, event):
        await self.send_json(event['data'])
