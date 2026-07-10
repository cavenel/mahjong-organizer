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

from mahj.models import Player, Schedule, Seat, TournamentSettings, Tenant

TEMPLATE = 'mahj/static/MahjongTemplate.xlsx'


@pytest.fixture
def imp_tenant(db):
    return Tenant.objects.create(name='Import', subdomain='imp')


@pytest.fixture
def staff_client(imp_tenant):
    c = Client()
    c.defaults['HTTP_HOST'] = 'imp.example.com'  # -> subdomain 'imp'
    u = User.objects.create_superuser('imp_staff', password='pw')
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


def _snapshot(tenant):
    players = {
        (p.full_name, p.EMA_ID, p.country, p.team, p.email, p.draw_number)
        for p in Player.objects.filter(tenant=tenant)
    }
    seats = {
        (s.round_nb, s.table_nb, s.wind, s.draw_number)
        for s in Seat.objects.filter(tenant=tenant)
    }
    schedule = {
        (i.day, i.time, i.name, i.is_round)
        for i in Schedule.objects.filter(tenant=tenant)
    }
    return players, seats, schedule


def test_export_round_trips_through_import(staff_client, imp_tenant):
    """Import a tournament, add per-player extras (team + email) and a schedule,
    export via admin_export_to_template, then re-import: the roster (incl. team and
    email), the full seating chart and the schedule (incl. is_round) must match."""
    staff_client.post('/admin_upload_from_template', {'myfile': _filled_workbook(16)})

    # Populate fields beyond name/EMA/draw so the round-trip is actually exercised.
    for i, p in enumerate(Player.objects.filter(tenant=imp_tenant).order_by('id')):
        p.team = f'Team{i % 4}'
        p.email = f'player{i}@example.com'
        p.save()

    before = _snapshot(imp_tenant)

    resp = staff_client.get('/admin_export_to_template')
    assert resp.status_code == 200
    assert resp['Content-Type'] == \
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    # A single seating sheet for this field size, not the template's full set.
    wb = load_workbook(io.BytesIO(resp.content))
    assert wb.sheetnames == ['Options', 'Players', 'Schedule', '16 players']

    exported = io.BytesIO(resp.content)
    exported.seek(0)
    exported.name = 'template.xlsx'

    resp = staff_client.post('/admin_upload_from_template', {'myfile': exported})
    assert resp.status_code in (200, 302)

    assert _snapshot(imp_tenant) == before


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


# --- robustness / hardening (PR A: F-C1, F-H9, F-M11, F-M12) ---------------

_KEEP = object()  # sentinel: leave the template's default number of rounds


def _workbook(rows, nb_rounds=_KEEP):
    """Build an importable workbook from `rows` — a list of
    (last, first, ema, country, team, rand) tuples written into the Players sheet
    (a None cell is left blank; an all-None tuple is a blank spacer row). Pass
    `nb_rounds` (incl. None for a blank cell) to override the Options 'number of
    rounds' cell (B3); omit it to keep the template default."""
    wb = load_workbook(TEMPLATE)
    ps = wb['Players']
    for i, cells in enumerate(rows):
        for c, val in enumerate(cells, start=1):
            if val is not None:
                ps.cell(row=i + 2, column=c, value=val)
    if nb_rounds is not _KEEP:
        # Assign .value directly: cell(..., value=None) is a no-op in openpyxl.
        wb['Options'].cell(row=3, column=2).value = nb_rounds
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    buf.name = 'template.xlsx'
    return buf


def _rows16(**overrides):
    """16 complete players, draw numbers 1..16. `overrides` is {row_index: tuple}
    to replace individual rows (0-based over the 16)."""
    rows = [(f'Last{i + 1}', f'First{i + 1}', 90000 + i, 'Sweden', None, i + 1)
            for i in range(16)]
    for idx, tup in overrides.items():
        rows[idx] = tup
    return rows


def test_blank_row_midroster_does_not_truncate(staff_client, imp_tenant):
    """F-H9: a fully-blank spacer row in the middle of the roster is skipped, not
    treated as the end of the roster (which used to drop everyone below it)."""
    rows = _rows16()
    rows.insert(8, (None, None, None, None, None, None))  # blank spacer mid-roster
    resp = staff_client.post('/admin_upload_from_template', {'myfile': _workbook(rows)})
    assert resp.status_code in (200, 302)
    assert Player.objects.filter(tenant=imp_tenant).count() == 16


def test_team_whitespace_collapsed_and_numeric_cell(staff_client, imp_tenant):
    """F-M11 + F-M12a: internal whitespace is collapsed so "Team  A" and "Team A"
    are one team; a numeric team cell is coerced to a string instead of crashing;
    case is NOT folded ("sweden" and "Sweden" stay two distinct teams). Every team
    here is a valid group of four (see test_uneven_team_rejected)."""
    teams = ['Team  A'] * 2 + ['Team A'] * 2 + [123] * 4 + ['sweden'] * 4 + ['Sweden'] * 4
    rows = [(f'Last{i + 1}', f'First{i + 1}', 90000 + i, 'Sweden', teams[i], i + 1)
            for i in range(16)]
    resp = staff_client.post('/admin_upload_from_template', {'myfile': _workbook(rows)})
    assert resp.status_code in (200, 302)
    stored = set(Player.objects.filter(tenant=imp_tenant).values_list('team', flat=True))
    assert stored == {'Team A', '123', 'sweden', 'Sweden'}
    # The two whitespace variants collapsed into one four-person "Team A".
    assert Player.objects.filter(tenant=imp_tenant, team='Team A').count() == 4


def test_uneven_team_rejected(staff_client, imp_tenant):
    """F-M11: teams are always groups of four, so a team of a different size is
    rejected (and per F-C1 the import leaves the tournament empty). This is what
    surfaces a typo/case split like "Sweden"(3) vs "sweden"(1)."""
    teams = ['Sweden'] * 3 + ['sweden'] * 1 + ['Norway'] * 4 + ['Denmark'] * 4 + ['Finland'] * 4
    rows = [(f'Last{i + 1}', f'First{i + 1}', 90000 + i, 'Sweden', teams[i], i + 1)
            for i in range(16)]
    resp = staff_client.post('/admin_upload_from_template', {'myfile': _workbook(rows)})
    assert resp.status_code == 200
    assert Player.objects.filter(tenant=imp_tenant).count() == 0


def test_absent_ema_left_blank(staff_client, imp_tenant):
    """F-M12c: a genuinely absent EMA id is silently blank (most players have none)."""
    rows = [(f'Last{i + 1}', f'First{i + 1}', None, 'Sweden', None, i + 1) for i in range(16)]
    resp = staff_client.post('/admin_upload_from_template', {'myfile': _workbook(rows)})
    assert resp.status_code in (200, 302)
    players = Player.objects.filter(tenant=imp_tenant)
    assert players.count() == 16
    assert all(p.EMA_ID == '' for p in players)


def test_invalid_ema_fails_whole_import(staff_client, imp_tenant):
    """F-M12c: a present-but-unparseable EMA id fails the import (and per F-C1
    leaves the tournament empty) rather than silently blanking the id."""
    rows = _rows16()
    rows[4] = ('LastX', 'FirstX', 'not-a-number', 'Sweden', None, 5)
    resp = staff_client.post('/admin_upload_from_template', {'myfile': _workbook(rows)})
    assert resp.status_code == 200
    assert Player.objects.filter(tenant=imp_tenant).count() == 0


def test_zero_draw_number_rejected(staff_client, imp_tenant):
    """F-M12d: draw number 0 collides with the empty-seat sentinel, so it is
    rejected (draw numbers are 1-based)."""
    rows = _rows16()
    rows[0] = ('Last1', 'First1', 90000, 'Sweden', None, 0)
    resp = staff_client.post('/admin_upload_from_template', {'myfile': _workbook(rows)})
    assert resp.status_code == 200
    assert Player.objects.filter(tenant=imp_tenant).count() == 0


def test_blank_rounds_rejected(staff_client, imp_tenant):
    """F-M12b: a blank/zero rounds count creates no seating and used to 'succeed'
    with nothing playable; it is now rejected."""
    resp = staff_client.post(
        '/admin_upload_from_template', {'myfile': _workbook(_rows16(), nb_rounds=None)})
    assert resp.status_code == 200
    assert Player.objects.filter(tenant=imp_tenant).count() == 0
    assert Seat.objects.filter(tenant=imp_tenant).count() == 0


def test_failed_import_leaves_tournament_empty(staff_client, imp_tenant):
    """F-C1: a failed re-import over a live tournament wipes it to a clean empty
    state (no partial/ghost tournament, and not silently reverted to the old one)."""
    staff_client.post('/admin_upload_from_template', {'myfile': _filled_workbook(16)})
    assert Player.objects.filter(tenant=imp_tenant).count() == 16

    rows = _rows16()
    rows[3] = ('LastBad', 'FirstBad', 'garbage', 'Sweden', None, 4)  # bad EMA -> fails
    staff_client.post('/admin_upload_from_template', {'myfile': _workbook(rows)})

    assert Player.objects.filter(tenant=imp_tenant).count() == 0
    assert Seat.objects.filter(tenant=imp_tenant).count() == 0
    ts = TournamentSettings.objects.get(tenant=imp_tenant)
    assert ts.nb_rounds == 0
    assert ts.fullname == ''
