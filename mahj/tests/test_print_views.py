"""Print outputs: the paper artefacts produced for the room.

These pages read the seating chart straight out of Seat rows and lay it out for
print, so they have no JSON/wire surface of their own — a broken context key just
renders a blank poster. Smoke-render them so a silent template variable miss is
caught here rather than on paper.
"""
import re

import pytest
from django.contrib.auth.models import User
from django.test import Client

from mahj.models import Seat
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


def test_table_posters_number_tables_past_the_first_page(staff_client, tournament):
    """Posters are chunked 20 to an A3 page. Numbering used to be derived from the
    nested page/row/column loop counters, and Django filters chain left-to-right,
    so page 2 came out numbered 101-120 instead of 21-40."""
    tenant = tournament['tenant']
    # Widen round 1 to 21 tables, so table 21 opens a second page on its own.
    for table_nb in range(5, 22):
        for wind in range(1, 5):
            Seat.objects.create(tenant=tenant, round_nb=1, table_nb=table_nb,
                                wind=wind, draw_number=1)

    body = staff_client.get('/table_posters').content.decode()
    numbers = re.findall(r'font-weight:bold;">(\d+)</td>', body)
    # Round 1 now runs 1..21; rounds 2 and 3 still hold 4 tables each.
    assert numbers[:21] == [str(n) for n in range(1, 22)]
    assert '101' not in numbers


def test_print_scores_masks_rounds_for_the_public(monkeypatch, tournament):
    """`/print_scores` renders whatever privilege the viewer has: an anonymous
    request must compute standings with full_view=False (published/non-withheld
    rounds only), an admin with full_view=True. Guards against the sheet leaking
    the withheld final during the pre-ceremony suspense window."""
    from mahj.views import print_views

    captured = []
    monkeypatch.setattr(
        print_views, 'scores_per_player_rows',
        lambda request, full_view=False, **kw: captured.append(full_view) or [])

    anon = Client()
    anon.defaults['HTTP_HOST'] = HOST
    assert anon.get('/print_scores').status_code == 200
    assert captured[-1] is False

    admin = Client()
    admin.defaults['HTTP_HOST'] = HOST
    u = User.objects.create_user('printadmin', password='pw')
    grant(u, tournament['tenant'], admin=True)
    admin.force_login(u)
    assert admin.get('/print_scores').status_code == 200
    assert captured[-1] is True


def test_print_schedule_lists_every_round(staff_client, tournament):
    """Smoke-render: the sheet groups the schedule by day, so a missing context key
    or an unbalanced tag prints a blank page rather than raising."""
    body = staff_client.get('/print_schedule').content.decode()
    for i in range(1, 4):
        assert f'Round {i}' in body
    assert body.count('</div>') == body.count('<div')


def test_player_cards_render_seat_wind_per_round(staff_client, tournament):
    """Each card lists the player's own wind per round (`player_wind`) plus the
    opponents at that table (`table_seats`)."""
    resp = staff_client.get('/player_cards')
    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'seat-cell' in body           # the "Tbl · Seat" badge row
    # player_wind is sliced to its initial, so a missing key would leave it blank
    assert any(f'class="wind {w}"' in body for w in 'ESWN'), body[:400]


# --------------------------------------------------------------------------
# S8: setup states must not 500 a public page
# --------------------------------------------------------------------------

@pytest.fixture
def client_():
    c = Client()
    c.defaults['HTTP_HOST'] = HOST
    return c


class TestPartialChartDoesNotCrash:
    """Both the grid builder and cross_positions assumed a complete, rectangular
    chart sized to `players // 4`. Real setup states break both assumptions."""

    def test_grid_holds_a_chart_wider_than_players_over_four(self, tournament):
        """A field that isn't a multiple of four leaves more tables than players//4,
        and writing a seat outside the grid raised IndexError."""
        from mahj.scoring.stats import scores_per_table
        tenant = tournament['tenant']
        # 16 players -> the old grid was 4 tables wide. Seat a fifth table.
        Seat.objects.create(tenant=tenant, round_nb=1, table_nb=5, wind=1, draw_number=1)
        grid = scores_per_table(tenant, tournament['settings'])
        assert len(grid[0]) >= 5
        assert grid[0][4][0]['seat'].table_nb == 5

    def test_grid_holds_a_chart_longer_than_nb_rounds(self, tournament):
        from mahj.scoring.stats import scores_per_table
        tenant = tournament['tenant']
        Seat.objects.create(tenant=tenant, round_nb=9, table_nb=1, wind=1, draw_number=1)
        grid = scores_per_table(tenant, tournament['settings'])
        assert len(grid) >= 9
        assert grid[8][0][0]['seat'].round_nb == 9

    def test_grid_still_renders_blank_tables_with_no_chart(self, tournament):
        """The player-count floor is kept, so an empty chart shows its blank tables."""
        from mahj.scoring.stats import scores_per_table
        tenant = tournament['tenant']
        Seat.objects.filter(tenant=tenant).delete()
        grid = scores_per_table(tenant, tournament['settings'])
        assert len(grid) == tournament['settings'].nb_rounds
        assert len(grid[0]) == 4          # 16 players // 4
        assert grid[0][0][0] == {}

    def test_cross_positions_survives_a_half_seated_table(self, client_, tournament):
        """An incomplete table leaves {} cells, and cell["seat"] on one of those
        raised KeyError."""
        tenant = tournament['tenant']
        Seat.objects.filter(tenant=tenant, round_nb=3, table_nb=4, wind__in=(3, 4)).delete()
        assert client_.get('/cross_positions').status_code == 200

    def test_cross_positions_per_team_survives_it_too(self, client_, tournament):
        tenant = tournament['tenant']
        for i, p in enumerate(tournament['players']):
            p.team = f'Team {chr(ord("A") + i % 4)}'
            p.save()
        Seat.objects.filter(tenant=tenant, round_nb=3, table_nb=4, wind__in=(3, 4)).delete()
        assert client_.get('/cross_positions?per_team=1').status_code == 200
