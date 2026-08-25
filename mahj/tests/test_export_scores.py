"""The admin scores export (admin_export_scores).

One workbook holding what has actually been entered: the standings table, every
played hand, and the per-seat numbers each sheet recorded. Admin-only, always
full view (unlike the public stats.xlsx, which is masked to published rounds),
and offered only once something is scored.
"""
import io

from openpyxl import load_workbook

from mahj.models import Hand, Player, Seat
from mahj.tests.conftest import client_for, role_user


def _sheets(content):
    """The workbook as {sheet name: [rows of values]}, header row included."""
    wb = load_workbook(io.BytesIO(content))
    return {name: [list(r) for r in wb[name].iter_rows(values_only=True)]
            for name in wb.sheetnames}


def _admin_client(tenant):
    c = client_for()
    c.force_login(role_user('exporter', tenant, admin=True))
    return c


def test_export_holds_scores_hands_and_seat_totals(tournament):
    tenant = tournament['tenant']
    client = _admin_client(tenant)

    resp = client.get('/admin_export_scores')
    assert resp.status_code == 200
    assert resp['Content-Type'] == \
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    assert resp['Content-Disposition'] == 'attachment; filename="T_scores.xlsx"'

    sheets = _sheets(resp.content)
    assert list(sheets) == ['Scores', 'Score sheets', 'Table results']

    # --- Scores: one row per ranked competitor, MCR so table points are carried.
    header, *rows = sheets['Scores']
    assert header[:4] == ['Rank', 'Player', 'EMA number', 'Country']
    # No teams in the fixture, so no Team column; three rounds x (table, MP, TP).
    assert 'Team' not in header
    assert header[4:] == ['Total TP', 'Total MP'] + [
        v for r in (1, 2, 3) for v in (f'R{r} table', f'R{r} MP', f'R{r} TP')
    ]
    assert len(rows) == 16
    assert [r[0] for r in rows] == sorted(r[0] for r in rows)   # ranked, best first

    # Every number is the one the standings show, seat by seat.
    top = rows[0]
    player = Player.objects.get(tenant=tenant, full_name=top[1])
    seats = {s.round_nb: s for s in Seat.objects.filter(
        tenant=tenant, draw_number=player.draw_number)}
    assert top[5] == sum(s.minipoints for s in seats.values() if s.minipoints is not None)
    for i, round_nb in enumerate((1, 2, 3)):
        table, mp, tp = top[6 + 3 * i:9 + 3 * i]
        seat = seats[round_nb]
        assert mp == seat.minipoints
        assert tp == seat.tablepoints
        # Round 3 is seated but unscored: no score, so no table either.
        assert table == (seat.table_nb if seat.minipoints is not None else None)

    # --- Score sheets: one row per played hand, winner and discarder named.
    header, *hand_rows = sheets['Score sheets']
    assert header == ['Round', 'Table', 'Hand', 'Result', 'Value',
                      'Winner wind', 'Winner', 'Dealt in wind', 'Dealt in by']
    assert len(hand_rows) == Hand.objects.filter(tenant=tenant).count()
    by_cell = {(r[0], r[1], r[2]): r for r in hand_rows}
    # The competitor in a seat is the Player holding its draw number (Seat has no
    # player FK), so resolve winner/discarder names the same way the export does.
    name_of = {p.draw_number: p.full_name for p in Player.objects.filter(tenant=tenant)}

    def seated_name(hand, wind):
        return name_of[Seat.objects.get(
            tenant=tenant, round_nb=hand.round_nb,
            table_nb=hand.table_nb, wind=wind).draw_number]

    for hand in Hand.objects.filter(tenant=tenant):
        row = by_cell[(hand.round_nb, hand.table_nb, hand.hand_nb)]
        assert row[4] == hand.points
        if hand.is_draw:
            assert row[3] == 'Draw'
            assert row[5] is None and row[6] is None      # nobody won it
        else:
            assert row[3] == 'Discard'                    # the fixture's wins all deal in
            assert row[6] == seated_name(hand, hand.win_by)
            assert row[8] == seated_name(hand, hand.win_from)

    # --- Table results: every seat in the chart, scored or not.
    header, *seat_rows = sheets['Table results']
    assert header == ['Round', 'Table', 'Validated', 'Wind', 'Draw #', 'Player',
                      'MP', 'Penalty', 'TP']
    assert len(seat_rows) == Seat.objects.filter(tenant=tenant).count()
    # Rounds 1-2 are validated sheets; round 3 has no sheet row at all.
    assert {r[2] for r in seat_rows if r[0] in (1, 2)} == {'yes'}
    assert {r[2] for r in seat_rows if r[0] == 3} == {'no'}
    # The unscored round exports blank cells, not zeros.
    assert {r[6] for r in seat_rows if r[0] == 3} == {None}


def test_penalties_are_exported_as_recorded(tournament):
    """The penalty is a sheet-balance figure that lives only on the Seat, so the
    export is the only place it appears beside the scores it explains."""
    tenant = tournament['tenant']
    seat = Seat.objects.filter(tenant=tenant, round_nb=1, table_nb=1, wind=1).get()
    seat.penalty = -30
    seat.save()

    sheets = _sheets(_admin_client(tenant).get('/admin_export_scores').content)
    row = next(r for r in sheets['Table results'][1:]
               if (r[0], r[1], r[3]) == (1, 1, 'E'))
    assert row[7] == -30


def test_riichi_export_omits_table_points(riichi_tournament):
    """Riichi ranks on minipoints alone and never fills table points in, so no
    column claims them."""
    sheets = _sheets(
        _admin_client(riichi_tournament['tenant']).get('/admin_export_scores').content)
    scores_header = sheets['Scores'][0]
    assert 'Total TP' not in scores_header
    assert not [h for h in scores_header if h and h.endswith('TP')]
    assert 'TP' not in sheets['Table results'][0]


def test_teams_add_a_column_when_the_tournament_uses_them(tournament):
    tenant = tournament['tenant']
    settings = tournament['settings']
    settings.has_teams = True
    settings.save()
    for i, p in enumerate(Player.objects.filter(tenant=tenant).order_by('id')):
        p.team = f'Team{i % 4}'
        p.save()

    sheets = _sheets(_admin_client(tenant).get('/admin_export_scores').content)
    header, *rows = sheets['Scores']
    assert header[4] == 'Team'
    assert {r[4] for r in rows} == {'Team0', 'Team1', 'Team2', 'Team3'}


def test_export_is_refused_before_anything_is_scored(tournament):
    """Nothing entered means nothing to export — the page hides the button, and a
    hand-typed URL is sent back to it rather than downloading an empty workbook."""
    tenant = tournament['tenant']
    Hand.objects.filter(tenant=tenant).delete()
    Seat.objects.filter(tenant=tenant).update(minipoints=None, tablepoints=None)

    resp = _admin_client(tenant).get('/admin_export_scores')
    assert resp.status_code == 302
    assert resp['Location'] == 'admin?page=import_template'


def test_the_page_only_offers_the_export_once_scoring_has_started(tournament):
    tenant = tournament['tenant']
    client = _admin_client(tenant)

    body = client.get('/admin?page=import_template').content
    assert b'admin_export_scores' in body

    Hand.objects.filter(tenant=tenant).delete()
    Seat.objects.filter(tenant=tenant).update(minipoints=None, tablepoints=None)
    body = client.get('/admin?page=import_template').content
    assert b'admin_export_scores' not in body


def test_export_is_admin_only(tournament, tenant_b):
    tenant = tournament['tenant']

    anon = client_for()
    assert anon.get('/admin_export_scores').status_code in (302, 403)

    scorer = client_for()
    scorer.force_login(role_user('sc', tenant, scorer=True))
    assert scorer.get('/admin_export_scores').status_code in (302, 403)

    # An admin of another tenant has no claim on this one's scores.
    outsider = client_for()
    outsider.force_login(role_user('outsider', tenant_b, admin=True))
    assert outsider.get('/admin_export_scores').status_code in (302, 403)
