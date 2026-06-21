"""Display WebSocket consumer: keepalive pong and publish/screen broadcasts.

These guard the path that drives auto-reload on public screens — a publish
must reach a client connected at /ws/display/, and the ping keepalive (used by
the client's half-open watchdog) must be answered.

No pytest-asyncio in this project, so each test wraps its async body in
asyncio.run() from an ordinary sync test.
"""
import asyncio

from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator

from mahj.consumers import TenantConsumer


def _display_communicator(subdomain='devsub'):
    comm = WebsocketCommunicator(TenantConsumer.as_asgi(), f'/ws/display/{subdomain}/')
    comm.scope['url_route'] = {'kwargs': {'subdomain': subdomain}}
    return comm


def test_ping_gets_pong():
    async def body():
        comm = _display_communicator()
        connected, _ = await comm.connect()
        assert connected
        await comm.send_json_to({'type': 'ping'})
        resp = await asyncio.wait_for(comm.receive_json_from(), timeout=2)
        assert resp == {'event': 'pong'}
        # An unrecognised client message is ignored, not answered or crashed.
        await comm.send_json_to({'type': 'something-else'})
        assert await comm.receive_nothing(timeout=0.3)
        await comm.disconnect()

    asyncio.run(body())


def test_broadcast_swallows_send_failure(monkeypatch):
    """A messaging failure (e.g. a transient Redis blip) must not propagate.
    Broadcasts run after the DB write in score-entry/publish paths, so a raise
    here would turn an already-committed save into a 500 for the scorer."""
    from mahj import signals

    class BoomLayer:
        async def group_send(self, *args, **kwargs):
            raise RuntimeError("redis down")

    monkeypatch.setattr('channels.layers.get_channel_layer', lambda: BoomLayer())

    # None of these should raise.
    signals.broadcast_scorer_row('devsub', {'round_nb': 1, 'table_nb': 1, 'positions': []})
    signals.broadcast_display('devsub', 'screen.update', {'event': 'screen_update'})
    signals.invalidate_leaderboard('devsub')


def test_publish_and_screen_broadcasts_reach_display_client():
    async def body():
        layer = get_channel_layer()
        comm = _display_communicator()
        assert (await comm.connect())[0]

        # Publish/unpublish fires on the leaderboard group.
        await layer.group_send('leaderboard_devsub', {
            'type': 'leaderboard.update', 'data': {'event': 'leaderboard_update'},
        })
        resp = await asyncio.wait_for(comm.receive_json_from(), timeout=2)
        assert resp == {'event': 'leaderboard_update'}

        # Screen switches fire on the display group.
        await layer.group_send('display_devsub', {
            'type': 'screen.update', 'data': {'event': 'screen_update'},
        })
        resp = await asyncio.wait_for(comm.receive_json_from(), timeout=2)
        assert resp == {'event': 'screen_update'}

        await comm.disconnect()

    asyncio.run(body())
