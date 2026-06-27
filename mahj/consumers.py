from channels.generic.websocket import AsyncJsonWebsocketConsumer


class TenantConsumer(AsyncJsonWebsocketConsumer):
    """Consumer for public displays (desktop, screen).

    Joins:
      leaderboard_{subdomain} — fired on round publish/unpublish only.
      display_{subdomain}     — screen switches, variable changes, counter updates.
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

    async def variables_update(self, event):
        await self.send_json(event['data'])

    async def counter_update(self, event):
        await self.send_json(event['data'])

    async def ceremony_update(self, event):
        await self.send_json(event['data'])


class ScorersConsumer(AsyncJsonWebsocketConsumer):
    """Consumer for scorer pages: live row sync and publish-toggle sync."""

    async def connect(self):
        subdomain = self.scope['url_route']['kwargs']['subdomain']
        self.group = f'scorers_{subdomain}'
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group, self.channel_name)

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
