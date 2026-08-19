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
from django.contrib.auth.models import AnonymousUser, User

from mahj.consumers import ScorersConsumer, TenantConsumer, scorer_socket_allowed


def _display_communicator(subdomain='devsub'):
    comm = WebsocketCommunicator(TenantConsumer.as_asgi(), f'/ws/display/{subdomain}/')
    comm.scope['url_route'] = {'kwargs': {'subdomain': subdomain}}
    return comm


def _scorers_communicator(user, subdomain='devsub'):
    comm = WebsocketCommunicator(ScorersConsumer.as_asgi(), f'/ws/scorers/{subdomain}/')
    comm.scope['url_route'] = {'kwargs': {'subdomain': subdomain}}
    comm.scope['user'] = user
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
    signals.broadcast_scorer_row('devsub', {'round_nb': 1, 'table_nb': 1, 'seats': []})
    signals.broadcast_display('devsub', 'screen.update', {'event': 'screen_update'})
    signals.invalidate_leaderboard('devsub')


class TestScorerSocketPolicy:
    """The scorers stream carries unpublished live scores, so joining is gated
    like the HTTP scoring endpoints. These exercise the pure policy function
    synchronously (no socket/thread), so DB visibility is deterministic."""

    def test_anonymous_denied(self, db):
        assert scorer_socket_allowed(AnonymousUser(), 'test') is False
        assert scorer_socket_allowed(None, 'test') is False

    def test_superuser_allowed_without_membership(self, db):
        su = User.objects.create_superuser('root', 'r@x.io', 'pw')
        assert scorer_socket_allowed(su, 'test') is True

    def test_scorer_member_allowed(self, tournament, grant_membership):
        u = User.objects.create_user('scorer', password='pw')
        grant_membership(u, tournament['tenant'], scorer=True)
        assert scorer_socket_allowed(u, tournament['tenant'].subdomain) is True

    def test_publisher_and_admin_allowed(self, tournament, grant_membership):
        pub = User.objects.create_user('pub', password='pw')
        grant_membership(pub, tournament['tenant'], publisher=True)
        adm = User.objects.create_user('adm', password='pw')
        grant_membership(adm, tournament['tenant'], admin=True)
        sub = tournament['tenant'].subdomain
        assert scorer_socket_allowed(pub, sub) is True
        assert scorer_socket_allowed(adm, sub) is True

    def test_display_op_only_denied(self, tournament, grant_membership):
        """A display operator has no business on the scorer stream."""
        u = User.objects.create_user('screen', password='pw')
        grant_membership(u, tournament['tenant'], display_op=True)
        assert scorer_socket_allowed(u, tournament['tenant'].subdomain) is False

    def test_member_of_other_tenant_denied(self, tournament, grant_membership):
        from mahj.models import Tenant
        other = Tenant.objects.create(name='Other', subdomain='other')
        u = User.objects.create_user('cross', password='pw')
        grant_membership(u, other, admin=True)  # admin, but of the WRONG tenant
        assert scorer_socket_allowed(u, tournament['tenant'].subdomain) is False

    def test_unknown_subdomain_denied(self, db):
        u = User.objects.create_user('nobody', password='pw')
        assert scorer_socket_allowed(u, 'no-such-tenant') is False


def test_scorers_socket_refuses_anonymous():
    """End-to-end: the real consumer closes an anonymous connection (this path
    short-circuits before any ORM access, so it's safe with :memory: sqlite)."""
    async def body():
        comm = _scorers_communicator(AnonymousUser())
        connected, _ = await comm.connect()
        assert not connected
        await comm.disconnect()

    asyncio.run(body())


def test_scorers_socket_accepts_superuser():
    """End-to-end: a superuser is accepted by the real consumer. The superuser
    bypass returns before any ORM access, so an unsaved User instance (which
    already reports is_authenticated=True) suffices — no DB, no cross-thread
    visibility concern with :memory: sqlite."""
    async def body():
        comm = _scorers_communicator(User(username='wsroot', is_superuser=True))
        connected, _ = await comm.connect()
        assert connected
        await comm.disconnect()

    asyncio.run(body())


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
