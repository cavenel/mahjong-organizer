import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from mahj.models import Player, Seat
from mahj.tests.conftest import grant, json_script_payload

HOST = 'test.example.com'


@pytest.fixture
def client_():
    c = Client()
    c.defaults['HTTP_HOST'] = HOST
    return c


@pytest.fixture
def staff(tournament):
    u = User.objects.create_user('boss', password='pw')
    grant(u, tournament['tenant'], admin=True)
    return u


@pytest.fixture
def undrawn(tournament):
    """The standard fixture draws everyone in; the live individual draw starts
    from an empty draw, so clear the numbers first."""
    Player.objects.filter(tenant=tournament['tenant']).update(draw_number=None)
    return tournament


def _assign(client_, player_id, draw_number):
    return client_.post(
        '/admin_player_draw_assign',
        data=json.dumps({'player_id': player_id, 'draw_number': draw_number}),
        content_type='application/json',
    )


def test_page_renders_for_staff(client_, staff, undrawn):
    client_.force_login(staff)
    resp = client_.get('/admin_player_draw')
    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'players-data' in body
    assert 'draw-numbers-data' in body


def test_page_forbidden_for_non_staff(client_, undrawn):
    # Anonymous is redirected to login, not served the page.
    resp = client_.get('/admin_player_draw')
    assert resp.status_code in (302, 403)


def test_page_warns_when_no_seating(client_, staff, undrawn):
    client_.force_login(staff)
    Seat.objects.filter(tenant=undrawn['tenant']).delete()
    resp = client_.get('/admin_player_draw')
    assert resp.status_code == 200
    assert b'No seating chart yet' in resp.content


def test_assign_sets_draw_number(client_, staff, undrawn):
    client_.force_login(staff)
    player = Player.objects.filter(tenant=undrawn['tenant']).first()
    resp = _assign(client_, player.id, 5)
    assert resp.status_code == 200
    assert resp.json()['ok'] is True
    player.refresh_from_db()
    assert player.draw_number == 5


def test_assign_rejects_taken_number(client_, staff, undrawn):
    client_.force_login(staff)
    players = list(Player.objects.filter(tenant=undrawn['tenant'])[:2])
    assert _assign(client_, players[0].id, 7).json()['ok'] is True
    resp = _assign(client_, players[1].id, 7)
    assert resp.status_code == 409
    body = resp.json()
    assert body['ok'] is False
    assert players[0].full_name in body['error']
    players[1].refresh_from_db()
    assert players[1].draw_number is None


def test_assign_rejects_invalid_number(client_, staff, undrawn):
    client_.force_login(staff)
    player = Player.objects.filter(tenant=undrawn['tenant']).first()
    # There are 16 seats (draw numbers 1..16); 99 is not a slot.
    resp = _assign(client_, player.id, 99)
    assert resp.status_code == 400
    assert resp.json()['ok'] is False
    player.refresh_from_db()
    assert player.draw_number is None


def test_reassigning_same_number_to_holder_is_ok(client_, staff, undrawn):
    client_.force_login(staff)
    player = Player.objects.filter(tenant=undrawn['tenant']).first()
    assert _assign(client_, player.id, 3).json()['ok'] is True
    # Same competitor, same number again: not a conflict with themselves.
    assert _assign(client_, player.id, 3).json()['ok'] is True


def test_clear_via_null_frees_the_number(client_, staff, undrawn):
    client_.force_login(staff)
    player = Player.objects.filter(tenant=undrawn['tenant']).first()
    _assign(client_, player.id, 9)
    resp = _assign(client_, player.id, None)
    assert resp.status_code == 200
    assert resp.json()['ok'] is True
    player.refresh_from_db()
    assert player.draw_number is None
    # And the freed number can now go to someone else.
    other = Player.objects.filter(tenant=undrawn['tenant']).exclude(id=player.id).first()
    assert _assign(client_, other.id, 9).json()['ok'] is True


def test_assign_get_not_allowed(client_, staff, undrawn):
    client_.force_login(staff)
    resp = client_.get('/admin_player_draw_assign')
    assert resp.status_code == 405


# ─── Escaping (F4 / F5) ────────────────────────────────────────────────────
# Player names are operator-entered (Excel import, player editor), and the page
# both embeds them as JSON and rebuilds HTML from them client-side.

HOSTILE_NAME = '</script><script>alert(1)</script>'


def test_hostile_player_name_cannot_close_the_script_block(client_, staff, undrawn):
    """json.dumps escapes neither `<` nor `/`, so this name used to terminate the
    surrounding <script> and inject live script. json_script escapes it."""
    client_.force_login(staff)
    player = Player.objects.filter(tenant=undrawn['tenant']).first()
    player.full_name = HOSTILE_NAME
    player.save()

    body = client_.get('/admin_player_draw').content.decode()
    assert HOSTILE_NAME not in body
    assert '\\u003C/script\\u003E' in body      # escaped, so inert
    # The page still receives the real name to display.
    assert HOSTILE_NAME in [p['full_name'] for p in json_script_payload(body, 'players-data')]


def test_page_escapes_names_before_they_reach_innerhtml(client_, staff, undrawn):
    """The page builds its search results, banner, reveal card and progress grid
    as HTML strings from the name, so it must load the escaping helper."""
    client_.force_login(staff)
    body = client_.get('/admin_player_draw').content.decode()
    assert 'js/browser_utils.js' in body
    assert 'escapeHtml(p.full_name)' in body


def test_concurrent_assign_of_the_same_number_is_409_not_500(client_, staff, undrawn):
    """Two registration desks handing out the same number at the same moment.

    When a number has no holder yet there is no row for select_for_update to lock,
    so both requests pass the availability check and the per-tenant unique
    constraint rejects the loser. That used to surface as an unhandled
    IntegrityError (500); it must be the same 409 the page already handles.
    """
    from unittest import mock
    from django.db import IntegrityError

    client_.force_login(staff)
    players = list(Player.objects.filter(tenant=undrawn['tenant'])[:2])
    # The other desk got there first, between our check and our write.
    real_save = Player.save

    def save_once_then_conflict(self, *a, **kw):
        Player.objects.filter(pk=players[1].pk).update(draw_number=4)
        Player.save = real_save
        raise IntegrityError('duplicate key value violates unique constraint')

    with mock.patch.object(Player, 'save', save_once_then_conflict):
        resp = _assign(client_, players[0].id, 4)

    assert resp.status_code == 409
    body = resp.json()
    assert body['ok'] is False
    assert '#4 is already taken by' in body['error']
    # And it names whoever actually holds it, so the page can say so.
    assert body['holder'] == players[1].full_name
    players[0].refresh_from_db()
    assert players[0].draw_number is None


def test_race_409_still_names_someone_when_the_winner_vanished(client_, staff, undrawn):
    """Defensive: if the conflicting row is gone by the time we re-read, the message
    still has to make sense rather than crash on None."""
    from unittest import mock
    from django.db import IntegrityError

    client_.force_login(staff)
    player = Player.objects.filter(tenant=undrawn['tenant']).first()

    def always_conflict(self, *a, **kw):
        raise IntegrityError('duplicate key')

    with mock.patch.object(Player, 'save', always_conflict):
        resp = _assign(client_, player.id, 6)

    assert resp.status_code == 409
    assert resp.json()['error'] == '#6 is already taken by someone else'


class TestMalformedAssignPayloads:
    """The registration desk's page always sends numbers, so anything else is a
    client bug — but it used to come back as a 500, and the page reads `error`
    off a JSON body, so the operator got a dead button and no reason."""

    @pytest.mark.parametrize('body', [
        {'player_id': 'x', 'draw_number': 1},
        {'player_id': None, 'draw_number': 1},
        {'player_id': [], 'draw_number': 1},
        {'draw_number': 1},
    ])
    def test_unreadable_player_id_is_a_named_400(self, client_, staff, undrawn, body):
        client_.force_login(staff)
        resp = client_.post('/admin_player_draw_assign', data=json.dumps(body),
                            content_type='application/json')
        assert resp.status_code == 400
        assert resp.json()['error'].startswith('player_id')

    @pytest.mark.parametrize('draw_number', ['x', [], {}, [1]])
    def test_unreadable_draw_number_is_a_named_400(
            self, client_, staff, undrawn, draw_number):
        client_.force_login(staff)
        player = undrawn['players'][0]
        resp = client_.post(
            '/admin_player_draw_assign',
            data=json.dumps({'player_id': player.id, 'draw_number': draw_number}),
            content_type='application/json')
        assert resp.status_code == 400
        assert resp.json()['error'].startswith('draw_number')
        assert Player.objects.get(pk=player.id).draw_number is None

    def test_a_numeric_string_still_assigns(self, client_, staff, undrawn):
        """The coercion is a widening, not a new rejection: the page's own
        payloads keep working, including a number that arrived as a string."""
        client_.force_login(staff)
        player = undrawn['players'][0]
        resp = _assign(client_, str(player.id), '7')
        assert resp.status_code == 200, resp.content
        assert resp.json()['ok'] is True
        assert Player.objects.get(pk=player.id).draw_number == 7

    def test_clearing_still_works(self, client_, staff, undrawn):
        """A null draw number is the undo path, not a malformed one."""
        client_.force_login(staff)
        player = undrawn['players'][0]
        _assign(client_, player.id, 7)
        resp = _assign(client_, player.id, None)
        assert resp.status_code == 200, resp.content
        assert Player.objects.get(pk=player.id).draw_number is None
