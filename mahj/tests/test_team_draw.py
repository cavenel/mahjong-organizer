import json
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import Client

from mahj.models import Player

HOST = 'test.example.com'


@pytest.fixture
def client_():
    c = Client()
    c.defaults['HTTP_HOST'] = HOST
    return c


@pytest.fixture
def staff(db):
    return User.objects.create_user('boss', password='pw', is_staff=True)


def _save(client_, assignments, **kwargs):
    return client_.post(
        '/admin_team_draw_save',
        data=json.dumps({'assignments': assignments}),
        content_type='application/json',
        **kwargs,
    )


def _draw_map(tenant):
    return {p.id: p.draw_number for p in Player.objects.filter(tenant=tenant)}


def test_save_reassigns_all_numbers(client_, staff, tournament):
    client_.force_login(staff)
    players = tournament['players']  # drawn 1..16 by the fixture
    # Reverse the draw: player with number n now gets 17-n. New numbers overlap
    # old ones, so this exercises the clear-first sequence.
    assignments = [{'player_id': p.id, 'rand_id': 16 - i} for i, p in enumerate(players)]
    resp = _save(client_, assignments)
    assert resp.status_code == 200
    for i, p in enumerate(players):
        p.refresh_from_db()
        assert p.draw_number == 16 - i


def test_duplicate_draw_number_rejected(client_, staff, tournament):
    client_.force_login(staff)
    players = tournament['players']
    before = _draw_map(tournament['tenant'])
    # Two competitors handed the same number.
    assignments = [{'player_id': players[0].id, 'rand_id': 5},
                   {'player_id': players[1].id, 'rand_id': 5}]
    resp = _save(client_, assignments)
    assert resp.status_code == 400
    assert _draw_map(tournament['tenant']) == before  # nothing written


def test_duplicate_player_rejected(client_, staff, tournament):
    client_.force_login(staff)
    players = tournament['players']
    before = _draw_map(tournament['tenant'])
    assignments = [{'player_id': players[0].id, 'rand_id': 3},
                   {'player_id': players[0].id, 'rand_id': 4}]
    resp = _save(client_, assignments)
    assert resp.status_code == 400
    assert _draw_map(tournament['tenant']) == before


def test_orphan_draw_number_rejected(client_, staff, tournament):
    client_.force_login(staff)
    players = tournament['players']
    before = _draw_map(tournament['tenant'])
    # There are 16 seats (draw numbers 1..16); 99 belongs to no seat.
    assignments = [{'player_id': players[0].id, 'rand_id': 99}]
    resp = _save(client_, assignments)
    assert resp.status_code == 400
    assert b'not a valid seat' in resp.content
    assert _draw_map(tournament['tenant']) == before


def test_unknown_player_rejected(client_, staff, tournament):
    client_.force_login(staff)
    before = _draw_map(tournament['tenant'])
    assignments = [{'player_id': 999999, 'rand_id': 1}]
    resp = _save(client_, assignments)
    assert resp.status_code == 400
    assert _draw_map(tournament['tenant']) == before


def test_malformed_body_rejected(client_, staff, tournament):
    client_.force_login(staff)
    resp = client_.post('/admin_team_draw_save', data='not json',
                        content_type='application/json')
    assert resp.status_code == 400


def test_mid_write_failure_rolls_back(client_, staff, tournament):
    client_.force_login(staff)
    players = tournament['players']
    before = _draw_map(tournament['tenant'])
    assignments = [{'player_id': p.id, 'rand_id': 16 - i} for i, p in enumerate(players)]
    # A valid batch can't hit the unique constraint, so force the final write to
    # fail and assert the clear-first steps rolled back with it (the draw survives).
    with mock.patch.object(Player.objects, 'bulk_update', side_effect=IntegrityError('boom')):
        with pytest.raises(IntegrityError):
            _save(client_, assignments)
    assert _draw_map(tournament['tenant']) == before


def test_get_not_allowed(client_, staff, tournament):
    client_.force_login(staff)
    resp = client_.get('/admin_team_draw_save')
    assert resp.status_code == 405


def test_forbidden_for_non_staff(client_, tournament):
    resp = _save(client_, [])
    assert resp.status_code in (302, 403)
