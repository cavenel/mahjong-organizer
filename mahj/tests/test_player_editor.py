"""Building the player list in the app (player_editor_add / player_editor_delete).

The Excel importer is one way to bring Player rows into existence; these
endpoints are the other. Together with Tournament settings and the in-app
seating generator they make a tournament possible without any spreadsheet, so
the last test walks that whole path.
"""
import json

import pytest

from mahj.models import Player, Seat, TournamentSettings, Tenant
from mahj.tests.conftest import client_for, role_user
from mahj.views.admin_views import _assign_short_names


@pytest.fixture
def ed_tenant(db):
    return Tenant.objects.create(name='Editor', subdomain='ed')


@pytest.fixture
def admin_client_(ed_tenant):
    """A tenant admin (not a superuser, so Membership is exercised)."""
    c = client_for('ed.example.com')
    c.force_login(role_user('ed_admin', ed_tenant, admin=True))
    return c


def _post(client, url, body):
    return client.post(url, data=json.dumps(body), content_type='application/json')


def _add(client, count):
    return _post(client, '/player_editor_add', {'count': count})


def _names(tenant):
    return sorted(Player.objects.filter(tenant=tenant).values_list('full_name', flat=True))


# --- add ------------------------------------------------------------------------

def test_add_creates_placeholder_mononyms(admin_client_, ed_tenant):
    resp = _add(admin_client_, 3)
    assert resp.status_code == 200
    data = resp.json()
    assert data['ok'] is True
    assert [r['full_name'] for r in data['players']] == ['Player 1', 'Player 2', 'Player 3']
    # Response rows carry the editor's row shape.
    assert set(data['players'][0]) == {
        'id', 'draw_number', 'full_name', 'first_name', 'last_name', 'EMA_ID', 'country', 'team'}
    p = Player.objects.get(tenant=ed_tenant, full_name='Player 2')
    assert (p.first_name, p.last_name, p.short_name, p.draw_number) == ('Player 2', '', 'Player 2', None)


def test_add_numbers_from_the_current_roster_size(admin_client_, ed_tenant):
    _add(admin_client_, 2)
    _add(admin_client_, 2)
    assert _names(ed_tenant) == ['Player 1', 'Player 2', 'Player 3', 'Player 4']
    # Renaming rows doesn't change the count, so the next number is still 5 …
    Player.objects.filter(tenant=ed_tenant, full_name='Player 1').update(full_name='Ann Lee')
    assert [r['full_name'] for r in _add(admin_client_, 1).json()['players']] == ['Player 5']
    # … after a deletion the count drops, so numbering steps back — but never
    # onto a name someone already carries.
    Player.objects.get(tenant=ed_tenant, full_name='Ann Lee').delete()
    resp = _add(admin_client_, 2)
    assert [r['full_name'] for r in resp.json()['players']] == ['Player 6', 'Player 7']


@pytest.mark.parametrize('count', [0, 201, -3])
def test_add_rejects_out_of_range_count(admin_client_, ed_tenant, count):
    resp = _add(admin_client_, count)
    assert resp.status_code == 400
    assert 'between 1 and 200' in resp.json()['error']
    assert not Player.objects.filter(tenant=ed_tenant).exists()


def test_add_rejects_unreadable_count_as_400(admin_client_, ed_tenant):
    resp = _add(admin_client_, 'four')
    assert resp.status_code == 400
    assert not Player.objects.filter(tenant=ed_tenant).exists()


def test_add_caps_the_roster_at_200(admin_client_, ed_tenant):
    assert _add(admin_client_, 200).status_code == 200
    resp = _add(admin_client_, 1)
    assert resp.status_code == 400
    assert 'at most 200' in resp.json()['error']
    assert Player.objects.filter(tenant=ed_tenant).count() == 200


def test_add_requires_post(admin_client_, ed_tenant):
    assert admin_client_.get('/player_editor_add').status_code == 405


def test_add_forbidden_for_scorer(ed_tenant):
    c = client_for('ed.example.com')
    c.force_login(role_user('ed_scorer', ed_tenant, scorer=True))
    resp = _add(c, 4)
    assert resp.status_code == 403
    assert not Player.objects.filter(tenant=ed_tenant).exists()


def test_add_anonymous_is_redirected_to_login(ed_tenant):
    resp = _add(client_for('ed.example.com'), 4)
    assert resp.status_code in (302, 403)
    assert not Player.objects.filter(tenant=ed_tenant).exists()


# --- delete ---------------------------------------------------------------------

def test_delete_removes_player_and_frees_draw_number(admin_client_, ed_tenant):
    _add(admin_client_, 4)
    Seat.objects.bulk_create([
        Seat(tenant=ed_tenant, draw_number=d, round_nb=1, table_nb=1, wind=d)
        for d in range(1, 5)])
    a, b = Player.objects.filter(tenant=ed_tenant).order_by('id')[:2]
    Player.objects.filter(id=a.id).update(draw_number=2)

    resp = _post(admin_client_, '/player_editor_delete', {'id': a.id})
    assert resp.status_code == 200 and resp.json() == {'ok': True}
    assert not Player.objects.filter(id=a.id).exists()
    # The seat rows are untouched (keyed by draw number, not by FK) …
    assert Seat.objects.filter(tenant=ed_tenant).count() == 4
    # … and the freed number can be handed to someone else.
    resp = _post(admin_client_, '/admin_player_draw_assign', {'player_id': b.id, 'draw_number': 2})
    assert resp.status_code == 200
    assert Player.objects.get(id=b.id).draw_number == 2


def test_delete_unknown_id_is_404(admin_client_, ed_tenant):
    resp = _post(admin_client_, '/player_editor_delete', {'id': 999999})
    assert resp.status_code == 404
    assert resp.json()['ok'] is False


def test_delete_cannot_reach_another_tenant(admin_client_, ed_tenant):
    other = Tenant.objects.create(name='Other', subdomain='other')
    victim = Player.objects.create(tenant=other, full_name='Far Away', first_name='Far')
    resp = _post(admin_client_, '/player_editor_delete', {'id': victim.id})
    assert resp.status_code == 404
    assert Player.objects.filter(id=victim.id).exists()


def test_delete_requires_post(admin_client_, ed_tenant):
    assert admin_client_.get('/player_editor_delete').status_code == 405


# --- short names ----------------------------------------------------------------

def _p(first, last):
    return Player(first_name=first, last_name=last)


@pytest.mark.django_db  # Player() resolves the tenant FK default
def test_assign_short_names_disambiguates_shared_first_names():
    solo, a, b, mono = _p('Ann', 'Lee'), _p('Chris', 'Derek'), _p('Chris', 'Anders'), _p('Chris', '')
    _assign_short_names([solo, a, b, mono])
    assert (solo.short_name, a.short_name, b.short_name, mono.short_name) == \
        ('Ann', 'Chris D.', 'Chris A.', 'Chris')


@pytest.mark.django_db
def test_assign_short_names_grows_prefix_until_unique():
    a, b = _p('Chris', 'Derek'), _p('Chris', 'Dereck')
    _assign_short_names([a, b])
    assert (a.short_name, b.short_name) == ('Chris Derek.', 'Chris Derec.')


def test_editor_name_change_redisambiguates_the_whole_roster(admin_client_, ed_tenant):
    _add(admin_client_, 2)
    a, b = Player.objects.filter(tenant=ed_tenant).order_by('id')
    _post(admin_client_, '/player_editor_save',
          {'players': [{'id': a.id, 'first_name': 'Chris', 'last_name': 'Derek'}]})
    a.refresh_from_db()
    assert a.short_name == 'Chris'
    # Naming a second Chris changes the *first* one's token too.
    _post(admin_client_, '/player_editor_save',
          {'players': [{'id': b.id, 'first_name': 'Chris', 'last_name': 'Anders'}]})
    a.refresh_from_db(); b.refresh_from_db()
    assert (a.short_name, b.short_name) == ('Chris D.', 'Chris A.')
    assert (a.full_name, b.full_name) == ('Chris Derek', 'Chris Anders')


# --- the whole Excel-free path --------------------------------------------------

def test_tournament_without_excel(admin_client_, ed_tenant):
    """Settings → Add players → Seating → draw, and the dashboard follows along."""
    # 1. Settings: number of rounds (the seating generator reads it).
    resp = admin_client_.post('/admin?page=settings&action=set_tournament&tournament-nb_rounds=3&tournament-title=Club+Cup')
    assert resp.status_code == 200
    assert TournamentSettings.objects.get(tenant=ed_tenant).nb_rounds == 3

    # 2. Roster: 16 placeholders, rename one.
    assert _add(admin_client_, 16).status_code == 200
    first = Player.objects.filter(tenant=ed_tenant).order_by('id').first()
    assert _post(admin_client_, '/player_editor_save',
                 {'players': [{'id': first.id, 'first_name': 'Ann', 'last_name': 'Lee'}]}).status_code == 200

    # 3. Seating chart, generated in-app from the player count.
    page = admin_client_.get('/admin?page=seating')
    assert page.status_code == 200
    resp = _post(admin_client_, '/admin_generate_seating', {'apply': True})
    assert resp.status_code == 200 and resp.json()['ok'] is True
    assert Seat.objects.filter(tenant=ed_tenant).count() == 3 * 4 * 4
    assert Player.objects.filter(tenant=ed_tenant).count() == 16  # roster kept

    # 4. Draw one player in via the editor's draw column.
    resp = _post(admin_client_, '/admin_player_draw_assign', {'player_id': first.id, 'draw_number': 7})
    assert resp.status_code == 200
    assert Player.objects.get(id=first.id).draw_number == 7

    # 5. The dashboard's setup checklist reflects it — no import ever happened.
    dash = admin_client_.get('/admin?page=welcome').content.decode()
    assert '16</span> players listed' in dash
    assert 'Seating chart ready' in dash
    assert '1</span>/16 players drawn in' in dash


def test_editor_page_renders_add_controls_with_empty_roster(admin_client_, ed_tenant):
    html = admin_client_.get('/admin?page=player_editor').content.decode()
    assert 'data-testid="add-player"' in html
    assert 'player_editor_add' in html
    assert 'player_editor_delete' in html
