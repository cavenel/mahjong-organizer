"""Print outputs: the paper artefacts produced for the room.

These pages read the seating chart straight out of Seat rows and lay it out for
print, so they have no JSON/wire surface of their own — a broken context key just
renders a blank poster. Smoke-render them so a silent template variable miss is
caught here rather than on paper.
"""
import pytest
from django.contrib.auth.models import User
from django.test import Client

from mahj.tests.conftest import grant

HOST = 'test.example.com'


@pytest.fixture
def staff_client(tenant):
    c = Client()
    c.defaults['HTTP_HOST'] = HOST
    u = User.objects.create_user('printer', password='pw')
    grant(u, tenant, admin=True)
    c.force_login(u)
    return c


def test_table_posters_render_one_per_table_and_round(staff_client, tournament):
    """One poster per (round, table) — 3 rounds x 4 tables in the fixture — each
    labelled with the four seated players."""
    resp = staff_client.get('/table_posters')
    assert resp.status_code == 200
    body = resp.content.decode()
    assert body.count('class="name_top"') == 12
    # Seats resolve to real competitors, not empty "Player N" placeholders.
    assert 'Player1' in body


def test_player_cards_render_seat_wind_per_round(staff_client, tournament):
    """Each card lists the player's own wind per round (`player_wind`) plus the
    opponents at that table (`table_seats`)."""
    resp = staff_client.get('/player_cards')
    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'seat-cell' in body           # the "Tbl · Seat" badge row
    # player_wind is sliced to its initial, so a missing key would leave it blank
    assert any(f'class="wind {w}"' in body for w in 'ESWN'), body[:400]
