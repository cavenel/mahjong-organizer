"""Excel-import flow (admin_upload_from_template).

Guards the round-trip the admin console runs when an organizer uploads the
tournament template: it must create one Player per roster row (carrying the draw
number from the 'rand' column) and the full seating chart as Seat rows keyed by
draw number — with no Seat.player FK (the draw lives on Player.draw_number).
"""
import io

import pytest
from django.contrib.auth.models import User
from django.test import Client
from openpyxl import load_workbook

from mahj.models import Player, Seat, TournamentSettings, Tenant

TEMPLATE = 'mahj/static/MahjongTemplate.xlsx'


@pytest.fixture
def imp_tenant(db):
    return Tenant.objects.create(name='Import', subdomain='imp')


@pytest.fixture
def staff_client(imp_tenant):
    c = Client()
    c.defaults['HTTP_HOST'] = 'imp.example.com'  # -> subdomain 'imp'
    u = User.objects.create_user('imp_staff', password='pw', is_staff=True, is_superuser=True)
    c.force_login(u)
    return c


def _filled_workbook(n=16):
    """A copy of the shipped blank template with `n` roster rows filled in and a
    pre-assigned draw number (the 'rand' column) 1..n, so it exercises the fully
    pre-drawn import path."""
    wb = load_workbook(TEMPLATE)
    ps = wb['Players']
    for i in range(n):
        r = i + 2  # row 1 is the header
        ps.cell(r, 1, f'Last{i + 1}')     # last name
        ps.cell(r, 2, f'First{i + 1}')    # first name
        ps.cell(r, 3, 10000 + i)          # EMA id
        ps.cell(r, 4, 'Sweden')           # country
        ps.cell(r, 5, '')                 # team
        ps.cell(r, 6, i + 1)              # rand -> draw number
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    buf.name = 'template.xlsx'
    return buf


def test_import_creates_players_and_seats(staff_client, imp_tenant):
    resp = staff_client.post('/admin_upload_from_template', {'myfile': _filled_workbook(16)})
    assert resp.status_code in (200, 302)

    players = list(Player.objects.filter(tenant=imp_tenant))
    assert len(players) == 16
    # Every competitor carries their draw number (from the 'rand' column), unique.
    draw_numbers = sorted(p.draw_number for p in players)
    assert draw_numbers == list(range(1, 17))

    # Options sheet says 7 rounds; 16 players -> 4 tables -> 4 winds each.
    tournament = TournamentSettings.objects.get(tenant=imp_tenant)
    assert tournament.nb_rounds == 7
    seats = Seat.objects.filter(tenant=imp_tenant)
    assert seats.count() == 7 * 4 * 4  # rounds * tables * winds

    # A seat is keyed by draw number (no player FK); the competitor is the Player
    # holding that number.
    seat = seats.filter(round_nb=1, table_nb=1, wind=1).first()
    assert seat is not None
    assert Player.objects.filter(tenant=imp_tenant, draw_number=seat.draw_number).exists()
    assert not hasattr(seat, 'player_id')  # Seat has no player FK column


def test_import_without_rand_leaves_players_undrawn(staff_client, imp_tenant):
    """No 'rand' column -> roster imported but not yet drawn (draw_number NULL);
    the seating chart still exists, to be filled by randomize/team-draw."""
    wb = load_workbook(TEMPLATE)
    ps = wb['Players']
    for i in range(16):
        r = i + 2
        ps.cell(r, 1, f'Last{i + 1}')
        ps.cell(r, 2, f'First{i + 1}')
        ps.cell(r, 3, 20000 + i)
        ps.cell(r, 4, 'France')
        ps.cell(r, 5, '')
        # no rand
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    buf.name = 'template.xlsx'

    resp = staff_client.post('/admin_upload_from_template', {'myfile': buf})
    assert resp.status_code in (200, 302)

    assert Player.objects.filter(tenant=imp_tenant).count() == 16
    assert Player.objects.filter(tenant=imp_tenant, draw_number__isnull=True).count() == 16
    # Seats still created (structural chart), just not linked to a competitor yet.
    assert Seat.objects.filter(tenant=imp_tenant).count() == 7 * 4 * 4
