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
from mahj.tests.conftest import HOST, grant



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


# ── Player cards: format, duplex mirroring and the organiser's own styling ─────
#
# The cards are the one printout an organiser can restyle (Setup -> Player card
# design), so what is pinned here is the part they cannot see going wrong until
# it is on paper: which card lands on which half of which face, and that a
# stored design actually reaches the page.

def _sheets(body):
    """The card slots of each sheet, in print order, as their card labels."""
    return [
        re.findall(r'class="player-name"[^>]*>\s*([^<]*?)\s*<', sheet)
        for sheet in body.split('<div class="sheet">')[1:]
    ]


def test_player_cards_default_to_a6_portrait_four_up(staff_client, tournament):
    """The long-standing output: 4 cards per sheet, A6 portrait, classic theme."""
    body = staff_client.get('/player_cards').content.decode()
    assert 'format-a6_portrait' in body and 'theme-classic' in body
    # 16 players -> 4 front sheets, each followed by its back.
    assert body.count('<div class="sheet">') == 8
    assert len(_sheets(body)[0]) == 4


def test_player_cards_back_face_mirrors_the_front_for_duplex(staff_client, tournament):
    """Printing double-sided with a long-edge flip swaps the columns, so the back
    face has to swap them back or every card gets someone else's opponents."""
    sheets = _sheets(staff_client.get('/player_cards').content.decode())
    front, back = sheets[0], sheets[1]
    assert front == ['Player1 Lastname', 'Player2 Lastname',
                     'Player3 Lastname', 'Player4 Lastname']
    assert back == [front[1], front[0], front[3], front[2]]


def test_a6_cards_rotate_the_top_row(staff_client, tournament):
    """The top two cards print upside-down, so a sheet cut in half gives four
    cards that read the same way up."""
    body = staff_client.get('/player_cards').content.decode()
    first_sheet = body.split('<div class="sheet">')[1]
    assert first_sheet.count('rotate(180deg)') == 2


def test_a7_landscape_prints_eight_up_with_the_compact_header(staff_client, tournament):
    """A7 is half an A6, so it needs its own short header — not a scaled copy of
    the tall one, which would leave no room for the rounds."""
    s = tournament['settings']
    s.card_format = 'a7_landscape'
    s.save()
    body = staff_client.get('/player_cards').content.decode()
    assert 'format-a7_landscape' in body
    assert 'card-head compact' in body
    # 16 players, 8 per sheet -> 2 front sheets + 2 backs.
    assert body.count('<div class="sheet">') == 4
    sheets = _sheets(body)
    assert len(sheets[0]) == 8
    # Mirrored in pairs, same as A6, and nothing is rotated.
    assert sheets[1] == [sheets[0][i] for i in (1, 0, 3, 2, 5, 4, 7, 6)]
    assert 'rotate(180deg)' not in body


def test_an_unknown_stored_format_still_prints(staff_client, tournament):
    """A format retired in a later version must not 500 the print page."""
    s = tournament['settings']
    s.card_format = 'a3_banner'
    s.save(update_fields=['card_format'])
    body = staff_client.get('/player_cards').content.decode()
    assert 'class="sheet"' in body


def test_a_partly_filled_sheet_keeps_its_grid(staff_client, tournament):
    """Fewer cards than a sheet holds still prints a full grid of slots, or the
    remaining cards stretch to fill it and come out the wrong size."""
    body = staff_client.get('/player_cards?players=1&main=1').content.decode()
    first_sheet = body.split('<div class="sheet">')[1]
    assert first_sheet.count('class="card"') == 4          # 1 card + 3 blanks
    assert len(_sheets(body)[0]) == 1


def test_theme_defaults_reach_the_page(staff_client, tournament):
    """An organiser who never opened the design page still gets a full stylesheet:
    the base CSS defines no colours, so a missing default block prints a blank card."""
    body = staff_client.get('/player_cards').content.decode()
    assert '--accent:' in body and '--ink:' in body


def test_custom_css_is_emitted_last_so_it_wins(staff_client, tournament):
    s = tournament['settings']
    s.card_theme = 'bold'
    s.card_css = ':root { --accent: #123456; }\n.wind.E { background: red; }'
    s.save()
    body = staff_client.get('/player_cards').content.decode()
    assert '--accent: #123456' in body
    assert '.wind.E { background: red; }' in body
    # After the theme bundle, or the theme would override the organiser.
    assert body.index('.theme-bold') < body.index('--accent: #123456')


def test_preview_renders_one_card_cropped_to_the_card(staff_client, tournament):
    """The design page shows the card, not the A4 sheet it is tiled onto: three
    empty slots say nothing about a design. Same page and CSS, so it still
    matches what prints — and nothing is rotated, which would only be hard to
    read on screen."""
    body = staff_client.get('/player_cards?preview=1').content.decode()
    assert body.count('<div class="sheet">') == 2      # front and back
    assert len(_sheets(body)[0]) == 1                  # one card, no blanks
    assert ' preview"' in body                         # the crop-to-card class
    assert 'rotate(180deg)' not in body


def test_preview_crops_to_the_chosen_format(staff_client, tournament):
    """The cropped page is card-sized, so it has to follow the format."""
    body = staff_client.get('/player_cards?preview=1').content.decode()
    assert 'width: 105mm' in body and 'height: 143.5mm' in body
    s = tournament['settings']
    s.card_format = 'a7_landscape'
    s.save()
    body = staff_client.get('/player_cards?preview=1').content.decode()
    assert 'height: 71.75mm' in body


def test_wind_chip_letters_are_settable_and_fall_back(staff_client, tournament):
    """The chip letter colours are variables, not hardcoded.

    They were literals (white, and the accent for East) until an organiser asked
    where to change them. The base rules carry the old literals as var()
    fallbacks, so a card_css saved before the variables existed still prints the
    same rather than losing its letters.
    """
    body = staff_client.get('/player_cards').content.decode()
    assert '--wind-ink:' in body and '--wind-east-ink:' in body   # in the defaults
    assert 'var(--wind-ink, #FFFFFF)' in body                     # fallback kept

    s = tournament['settings']
    s.card_css = ':root { --accent: #006AA7; --accent-2: #FECC02; }'  # pre-variable block
    s.save()
    body = staff_client.get('/player_cards').content.decode()
    assert 'var(--wind-ink, #FFFFFF)' in body

    s.card_css = ':root { --wind-ink: #FF00AA; }'
    s.save()
    assert '--wind-ink: #FF00AA' in staff_client.get('/player_cards').content.decode()


def test_card_cells_never_wrap_and_boxes_take_the_slack(staff_client, tournament):
    """Two rules the eye catches on paper but no other test would.

    A cell that wraps costs a whole row of card height and reads as a bug, so
    nothing on a card wraps — and it is cut at the edge rather than ellipsised,
    because a value that only just overflows would lose two more characters to
    the "...". The writing boxes are fr columns so they absorb every millimetre
    the labels do not need: they were fixed-width, which left a strip of dead
    paper down the right of every card.
    """
    body = staff_client.get('/player_cards').content.decode()
    assert '.card { white-space: nowrap; }' in body
    assert 'text-overflow: clip' in body and 'text-overflow: ellipsis' not in body
    assert '3mm 18.5mm 11.5mm 1fr 1fr 1fr 1fr' in body


def test_each_format_supplies_its_own_header_and_footer(staff_client, tournament):
    """A compact card needs its own header and footer, not scaled copies: the A7
    prints the period/sessions/ruleset line at its foot because its one-band
    header has no room for it, while the A6 carries that in the header and prints
    only the spectator URL at the foot."""
    s = tournament['settings']
    body = staff_client.get('/player_cards').content.decode()
    assert 'Sessions' in body                      # A6: in the header
    a6_foot = body.count('class="footer-url mono"')

    s.card_format = 'a7_landscape'
    s.save()
    body = staff_client.get('/player_cards').content.decode()
    assert 'card-head compact' in body
    # A7: the same line, now in the foot — one per card face, not in the header.
    assert body.count('class="footer-url mono"') > 0 and a6_foot > 0
    head = body.split('class="footer-url mono"')[0]
    assert 'Sessions' not in head.rsplit('card-head compact', 1)[-1]


def test_the_preview_page_shrinks_on_a_phone_but_prints_true_to_size(staff_client, tournament):
    """A card is ~397px wide, wider than a phone screen.

    The preview page can be opened on its own (not only inside the design page's
    frame), so it scales itself down on a narrow screen rather than scrolling
    sideways — but only on screen: the print rules must keep true millimetres, or
    cards would come out of the printer undersized.
    """
    body = staff_client.get('/player_cards?preview=1').content.decode()
    screen_zoom = '@media screen and (max-width: 380px) { body.preview { zoom:'
    assert screen_zoom in body
    # The zoom steps are screen-only: nothing inside the print block scales.
    print_block = body.split('@media print')[1].split('</style>')[0]
    assert 'zoom' not in print_block
