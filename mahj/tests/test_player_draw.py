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
