"""Display admin page (admin?page=display): the mode-breakdown helpers that let
each saved mode show what it puts on every screen, plus the page rendering its
active-mode highlight.

A ScreenMode stores `views` as a JSON list of view strings in screen order — a
full-room snapshot: applying it sets every screen, padding with 'black' for
screens added after the mode was saved. The admin shows that breakdown and
marks the mode whose (padded) views match every screen's current view as active.
"""
import types

import pytest
import json
from django.contrib.auth.models import Group, User
from django.test import Client

from mahj.models import Schedule, Screen, ScreenMode, TournamentSettings
from mahj.views.admin_views import _mode_breakdowns, _pretty_view

HOST = 'test.example.com'


@pytest.mark.parametrize('view, label', [
    ('', 'Blank'),
    ('black', 'Blank'),
    ('null', 'Blank'),
    ('counter', 'Counter'),
    ('announcement', 'Announcement'),
    ('schedule', 'Schedule'),
    ('scores:detailed:all', 'Standings — detailed, all (rotating)'),
    ('scores:detailed', 'Standings — detailed, all (rotating)'),
    ('scores:totals:2', 'Standings — totals, page 2'),
    ('scores:totals:all', 'Standings — totals, all (rotating)'),
    # Unknown grammar falls through to the raw string rather than blanking.
    ('something-else', 'something-else'),
])
def test_pretty_view(view, label):
    assert _pretty_view(view) == label


def _mode(id, name, views):
    return types.SimpleNamespace(id=id, name=name, views=views)


def _screen(view, friendly_name=''):
    return types.SimpleNamespace(view=view, friendly_name=friendly_name)


def test_mode_breakdown_rows_and_active_match():
    screens = [_screen('scores:detailed:all'), _screen('counter')]
    modes = [
        _mode(1, 'Tournament', ['scores:detailed:all', 'counter']),
        _mode(2, 'Break', ['black', 'black']),
    ]
    out = _mode_breakdowns(modes, screens)

    tournament, break_ = out
    assert tournament['is_active'] is True
    assert break_['is_active'] is False
    assert [r['label'] for r in tournament['rows']] == ['/1', '/2']
    assert [r['pretty'] for r in tournament['rows']] == [
        'Standings — detailed, all (rotating)', 'Counter']


def test_mode_breakdown_normalizes_blank_views():
    """Empty/None views read as 'black' on both sides, so a saved all-blank mode
    matches screens whose stored view is the empty-string default."""
    screens = [_screen(''), _screen('')]
    modes = [_mode(1, 'Off', ['black', 'black'])]
    out = _mode_breakdowns(modes, screens)

    assert out[0]['is_active'] is True
    assert [r['pretty'] for r in out[0]['rows']] == ['Blank', 'Blank']


def test_mode_breakdown_fewer_views_than_screens():
    """A mode saved before a 4th screen was added: applying it blanks the surplus
    screen (set_mode pads with 'black'), so the row reads 'Blank' and the mode is
    active only when that screen is currently blank."""
    screens = [_screen('scores:detailed:all'), _screen('counter'),
               _screen('schedule'), _screen('black')]
    modes = [_mode(1, 'ThreeOfFour',
                   ['scores:detailed:all', 'counter', 'schedule'])]
    out = _mode_breakdowns(modes, screens)[0]

    assert out['is_active'] is True
    assert len(out['rows']) == 4
    assert out['rows'][3] == {'label': '/4', 'pretty': 'Blank'}


def test_mode_breakdown_surplus_screen_showing_content_is_not_active():
    """Same short mode, but the surplus screen shows live content: applying the
    mode would blank it, so the mode must NOT read as active."""
    screens = [_screen('scores:detailed:all'), _screen('counter')]
    modes = [_mode(1, 'OneOfTwo', ['scores:detailed:all'])]
    assert _mode_breakdowns(modes, screens)[0]['is_active'] is False


def test_mode_breakdown_covered_screen_differs_is_not_active():
    """If a screen the mode controls doesn't match, the mode isn't active."""
    screens = [_screen('counter'), _screen('schedule')]
    modes = [_mode(1, 'M', ['counter', 'black'])]
    assert _mode_breakdowns(modes, screens)[0]['is_active'] is False


def test_mode_breakdown_more_views_than_screens():
    """A mode saved with more screens than now exist: surplus views are dropped,
    and it's active when the remaining screens match."""
    screens = [_screen('counter')]
    modes = [_mode(1, 'Two', ['counter', 'schedule'])]
    out = _mode_breakdowns(modes, screens)[0]

    assert out['is_active'] is True
    assert len(out['rows']) == 1  # only as many rows as there are screens


def test_mode_breakdown_handles_malformed_views():
    """A non-list views value degrades to an empty mode: applying it blanks every
    screen, so every row reads 'Blank' and it isn't active while content shows."""
    screens = [_screen('counter')]
    modes = [_mode(1, 'Broken', None)]
    out = _mode_breakdowns(modes, screens)

    assert [r['pretty'] for r in out[0]['rows']] == ['Blank']
    assert out[0]['is_active'] is False
    assert out[0]['views_json'] == '[]'


def test_mode_breakdown_views_json_is_compact():
    """views_json must match JS JSON.stringify() byte-for-byte (no spaces) so the
    client-side active-mode comparison works."""
    screens = [_screen('scores:detailed:all'), _screen('counter')]
    modes = [_mode(1, 'T', ['scores:detailed:all', 'counter'])]
    assert _mode_breakdowns(modes, screens)[0]['views_json'] == \
        '["scores:detailed:all","counter"]'


def test_mode_breakdown_label_includes_friendly_name():
    """A renamed screen appends its name to the positional endpoint label."""
    screens = [_screen('counter', friendly_name='Main hall'), _screen('black')]
    modes = [_mode(1, 'T', ['counter', 'black'])]
    rows = _mode_breakdowns(modes, screens)[0]['rows']
    assert [r['label'] for r in rows] == ['/1 — Main hall', '/2']


# ── Page rendering ──────────────────────────────────────────────────────────

@pytest.fixture
def client_():
    c = Client()
    c.defaults['HTTP_HOST'] = HOST
    return c


@pytest.fixture
def display_op(db):
    u = User.objects.create_user('op', password='pw')
    group, _ = Group.objects.get_or_create(name='Display_op')
    u.groups.add(group)
    return u


def test_display_page_marks_active_mode(client_, display_op, tournament):
    tenant = tournament['tenant']
    Screen.objects.create(tenant=tenant, view='scores:detailed:all')
    Screen.objects.create(tenant=tenant, view='counter')
    ScreenMode.objects.create(
        tenant=tenant, name='Tournament',
        views=['scores:detailed:all', 'counter'])
    ScreenMode.objects.create(
        tenant=tenant, name='Break', views=['black', 'black'])

    client_.force_login(display_op)
    html = client_.get('/admin?page=display').content.decode()

    # Exactly one mode is highlighted as active, and the breakdown is rendered.
    # Match the applied class form ("mode-card mode-card--active"); the bare
    # ".mode-card--active" also appears in the <style> block and comments.
    assert html.count('mode-card mode-card--active') == 1
    assert 'Tournament' in html and 'Break' in html
    assert 'Standings — detailed, all (rotating)' in html
    # Screens are labelled by their positional endpoint (/1, /2…), not "Screen N".
    # Match the mode-breakdown row label markup so we don't trip on screen URLs.
    assert '>/1</span>' in html and '>/2</span>' in html


def test_display_page_no_active_mode_when_nothing_matches(client_, display_op, tournament):
    tenant = tournament['tenant']
    Screen.objects.create(tenant=tenant, view='schedule')
    ScreenMode.objects.create(tenant=tenant, name='Break', views=['black'])

    client_.force_login(display_op)
    html = client_.get('/admin?page=display').content.decode()

    assert 'mode-card mode-card--active' not in html


# ── Add mode ──────────────────────────────────────────────────────────────────

def test_views_field_is_unbounded():
    # add_mode stores the list of every screen's view, which grows past any
    # fixed CharField cap once there are many screens (a varchar(100) overflowed
    # in prod with a 500). The field must stay unbounded. SQLite ignores
    # max_length, so this model-level guard is what catches a regression.
    assert ScreenMode._meta.get_field('views').max_length is None


def test_add_mode_snapshots_all_screen_views(client_, display_op, tournament):
    tenant = tournament['tenant']
    # Enough screens with realistic view strings that the JSON snapshot is well
    # over the old 100-char cap.
    views = ['scores:detailed:all', 'standings', 'counter', 'schedule',
             'scores:totals:all', 'black', 'scores:detailed:5']
    for v in views:
        Screen.objects.create(tenant=tenant, view=v)
    client_.force_login(display_op)

    resp = client_.post('/admin?page=display&action=add_mode',
                        {'mode_name': 'Full house'})

    assert resp.status_code == 302
    mode = ScreenMode.objects.get(tenant=tenant, name='Full house')
    assert mode.views == views
    assert len(json.dumps(mode.views)) > 100


# ── Apply mode ────────────────────────────────────────────────────────────────

def test_set_mode_blanks_screens_added_after_the_mode_was_saved(client_, display_op, tournament):
    """A mode is a full-room snapshot: applying one sets every screen, so a
    screen added after the mode was saved goes blank rather than keeping stale
    content (e.g. live standings through a 'Break' mode)."""
    tenant = tournament['tenant']
    Screen.objects.create(tenant=tenant, view='counter')
    later = Screen.objects.create(tenant=tenant, view='scores:detailed:all')
    mode = ScreenMode.objects.create(tenant=tenant, name='Break', views=['black'])
    client_.force_login(display_op)

    resp = client_.post(f'/admin?page=display&set_mode={mode.id}',
                        HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    assert resp.status_code == 200
    later.refresh_from_db()
    assert later.view == 'black'
    assert [s['view'] for s in resp.json()['screens']] == ['black', 'black']


# ── Screen view endpoint ──────────────────────────────────────────────────────

def test_update_screen_view_bad_id_is_404(client_, display_op, tournament):
    """A missing, unknown or non-numeric ?id= must 404, not 500."""
    client_.force_login(display_op)
    for bad in ('999999', 'abc', ''):
        assert client_.post(f'/update_screen_view?id={bad}&view=black').status_code == 404


def test_update_screen_view_cannot_delete_screens(client_, display_op, tournament):
    """Screens are only deleted via the admin's remove-last action (positional
    /1, /2… addressing must stay stable): 'remove' is just a view string here,
    which index() renders as a blank screen."""
    tenant = tournament['tenant']
    screen = Screen.objects.create(tenant=tenant, view='counter')
    client_.force_login(display_op)

    resp = client_.post(f'/update_screen_view?id={screen.id}&view=remove')

    assert resp.status_code == 200
    screen.refresh_from_db()
    assert screen.view == 'remove'


# ── On-screen message ─────────────────────────────────────────────────────────

def test_welcome_is_stored_as_plain_text_with_newlines(client_, display_op, tournament):
    """The message is plain text end to end: newlines are stored as-is (the
    screens render them with white-space: pre-line), never as <br> HTML."""
    tenant = tournament['tenant']
    client_.force_login(display_op)

    resp = client_.post(
        '/admin?page=display&action=set_variable&variables-welcome=Lunch%20now%0ABack%20at%2014%3A00')

    assert resp.status_code == 200
    assert TournamentSettings.objects.get(tenant=tenant).welcome == 'Lunch now\nBack at 14:00'


# ── Screen rename ─────────────────────────────────────────────────────────────

@pytest.mark.django_db
@pytest.mark.parametrize('stored, expected', [
    ('Main hall', 'Main hall'),
    ('  Lobby  ', 'Lobby'),   # trimmed
    ('Unknown', ''),          # legacy placeholder reads as unnamed
    ('Screen_X', ''),         # legacy auto-name reads as unnamed
    ('', ''),
])
def test_screen_friendly_name(stored, expected):
    assert Screen(name=stored).friendly_name == expected


def test_update_screen_name_persists_and_clears(client_, display_op, tournament):
    tenant = tournament['tenant']
    screen = Screen.objects.create(tenant=tenant, view='counter')
    client_.force_login(display_op)

    resp = client_.get(f'/update_screen_name?id={screen.id}&name=Main+hall')
    assert resp.status_code == 200
    screen.refresh_from_db()
    assert screen.name == 'Main hall'

    # The renamed screen's label appears on the admin page as "/N — Name".
    html = client_.get('/admin?page=display').content.decode()
    assert 'Main hall' in html

    # An empty name clears it back to the bare positional label.
    client_.get(f'/update_screen_name?id={screen.id}&name=')
    screen.refresh_from_db()
    assert screen.friendly_name == ''


# ── set_variable error surfacing ────────────────────────────────────────────
# A save that can't fit the DB used to bubble up as a bare 500 the admin page
# swallowed silently. set_variable now validates length up front and returns a
# readable 400, which the page shows in an alert dialog.

def test_set_variable_rejects_over_long_message(client_, display_op, tournament):
    tenant = tournament['tenant']
    TournamentSettings.objects.filter(tenant=tenant).update(welcome='ok')
    client_.force_login(display_op)

    too_long = 'x' * 300  # welcome is max_length=255
    resp = client_.post(
        f'/admin?page=display&action=set_variable&variables-welcome={too_long}',
        {'csrfmiddlewaretoken': 'x'})

    assert resp.status_code == 400
    body = resp.content.decode()
    assert 'On-screen message is too long' in body
    assert '255' in body
    # The rejected value was not persisted.
    assert TournamentSettings.objects.get(tenant=tenant).welcome == 'ok'


def test_set_variable_saves_valid_message(client_, display_op, tournament):
    tenant = tournament['tenant']
    client_.force_login(display_op)

    resp = client_.post(
        '/admin?page=display&action=set_variable&variables-welcome=Round+3+starts+soon',
        {'csrfmiddlewaretoken': 'x'})

    assert resp.status_code == 200
    assert TournamentSettings.objects.get(tenant=tenant).welcome == 'Round 3 starts soon'


# ── Tournament settings page (admin?page=settings) ───────────────────────────
# Staff-only page exposing tournament identity (title/full name/city/period/
# rules), round count/length and the logo. The shared set_variable handler
# persists edits regardless of which page posts them.

@pytest.fixture
def staff(db):
    return User.objects.create_user('boss', password='pw', is_staff=True)


def test_settings_page_renders_identity_fields_for_staff(client_, staff, tournament):
    client_.force_login(staff)
    html = client_.get('/admin?page=settings').content.decode()
    assert 'Tournament settings' in html
    assert 'variables-title' in html
    assert 'variables-nb_rounds' in html
    # Round length + logo are staff surfaces, shown here.
    assert 'variables-total_time' in html


def test_settings_page_forbidden_for_non_staff(client_, display_op, tournament):
    """A display op reaching ?page=settings must get nothing (nav hides it, but
    the route has to enforce it too)."""
    client_.force_login(display_op)
    html = client_.get('/admin?page=settings').content.decode()
    assert 'variables-title' not in html


def test_settings_page_saves_identity_via_set_variable(client_, staff, tournament):
    tenant = tournament['tenant']
    client_.force_login(staff)
    resp = client_.post(
        '/admin?page=settings&action=set_variable&variables-city=Uppsala&variables-nb_rounds=9',
        {'csrfmiddlewaretoken': 'x'})
    assert resp.status_code == 200
    v = TournamentSettings.objects.get(tenant=tenant)
    assert v.city == 'Uppsala'
    assert v.nb_rounds == 9


def test_settings_page_renders_schedule_editor(client_, staff, tournament):
    """The schedule editor is seeded with the tenant's rows (day/time/name/is_round)."""
    client_.force_login(staff)
    html = client_.get('/admin?page=settings').content.decode()
    assert 'schedule-data' in html
    assert 'scheduleEditor()' in html
    # The fixture seeds three "Round N" rows, all flagged as playing rounds.
    data = json.loads(html.split('id="schedule-data"', 1)[1].split('>', 1)[1].split('</script>', 1)[0])
    assert [r['name'] for r in data] == ['Round 1', 'Round 2', 'Round 3']
    assert all(r['is_round'] for r in data)


def test_save_schedule_replaces_rows_and_reports_round_count(client_, staff, tournament):
    tenant = tournament['tenant']
    client_.force_login(staff)
    payload = json.dumps([
        {'day': 'Sat', 'time': '09:00', 'name': 'Registration', 'is_round': False},
        {'day': 'Sat', 'time': '10:00', 'name': 'Round 1', 'is_round': True},
        {'day': '', 'time': '', 'name': '', 'is_round': False},  # wholly blank → dropped
    ])
    resp = client_.post('/admin?page=settings&action=save_schedule',
                        {'csrfmiddlewaretoken': 'x', 'schedule': payload})
    assert resp.status_code == 200
    assert resp.json() == {'rounds': 1}
    rows = list(Schedule.objects.filter(tenant=tenant).order_by('id'))
    assert [(r.name, r.is_round) for r in rows] == [('Registration', False), ('Round 1', True)]


def test_save_schedule_forbidden_for_non_staff(client_, display_op, tournament):
    tenant = tournament['tenant']
    before = Schedule.objects.filter(tenant=tenant).count()
    client_.force_login(display_op)
    resp = client_.post('/admin?page=settings&action=save_schedule',
                        {'csrfmiddlewaretoken': 'x', 'schedule': '[]'})
    # Non-staff get the empty "None" page, not the save handler — rows untouched.
    assert Schedule.objects.filter(tenant=tenant).count() == before


def test_dashboard_warns_on_schedule_round_mismatch(client_, staff, tournament):
    """Fixture: nb_rounds=3 with three round-rows → no warning; bump nb_rounds → warn."""
    tenant = tournament['tenant']
    client_.force_login(staff)
    html = client_.get('/admin').content.decode()
    assert "Per-round times won't line up" not in html

    v = TournamentSettings.objects.get(tenant=tenant)
    v.nb_rounds = 5
    v.save()
    html = client_.get('/admin').content.decode()
    assert "Per-round times won't line up" in html


def test_dashboard_no_warning_without_schedule(client_, staff, tournament):
    """An empty schedule isn't 'wrong' — don't nag before one is set up."""
    tenant = tournament['tenant']
    Schedule.objects.filter(tenant=tenant).delete()
    client_.force_login(staff)
    html = client_.get('/admin').content.decode()
    assert "Per-round times won't line up" not in html


# ── Player editor (admin?page=player_editor) ─────────────────────────────────
# Staff-only inline table for correcting roster metadata. Every field autosaves;
# draw_number is editable here too and goes through admin_player_draw_assign.

def test_player_editor_renders_roster_for_staff(client_, staff, tournament):
    client_.force_login(staff)
    html = client_.get('/admin?page=player_editor').content.decode()
    assert 'Edit players' in html
    assert 'playerEditor()' in html
    assert 'Player1 Lastname' in html
    # Draw number is now an editable column backed by the valid-slot list.
    assert 'Draw number' in html
    assert 'valid-draw-data' in html
    assert 'changeDraw(' in html


def test_player_editor_forbidden_for_non_staff(client_, display_op, tournament):
    client_.force_login(display_op)
    html = client_.get('/admin?page=player_editor').content.decode()
    assert 'playerEditor()' not in html


def test_player_editor_save_persists_metadata(client_, staff, tournament):
    tenant = tournament['tenant']
    from mahj.models import Player
    p = Player.objects.filter(tenant=tenant).first()
    client_.force_login(staff)
    resp = client_.post(
        '/player_editor_save',
        data=json.dumps({'players': [{
            'id': p.id, 'full_name': 'Corrected Name', 'first_name': '',
            'country': 'Norway', 'EMA_ID': 'E99999', 'email': 'x@y.z', 'team': 'Reds',
        }]}),
        content_type='application/json')
    assert resp.status_code == 200
    p.refresh_from_db()
    assert p.full_name == 'Corrected Name'
    assert p.country == 'Norway'
    # Blank first name mirrors Player.save(): first token of the full name.
    assert p.first_name == 'Corrected'


def test_player_editor_save_ignores_unknown_ids(client_, staff, tournament):
    client_.force_login(staff)
    resp = client_.post(
        '/player_editor_save',
        data=json.dumps({'players': [{'id': 999999, 'full_name': 'Ghost'}]}),
        content_type='application/json')
    assert resp.status_code == 200


def test_player_editor_save_requires_staff(client_, display_op, tournament):
    client_.force_login(display_op)
    resp = client_.post(
        '/player_editor_save',
        data=json.dumps({'players': []}),
        content_type='application/json')
    # user_passes_test redirects non-staff to login rather than serving the view.
    assert resp.status_code in (302, 403)


# ── Dashboard (admin?page=welcome) ───────────────────────────────────────────
# The landing page: a setup checklist + round/timer progress, shown to every
# admin role.

def test_dashboard_shows_setup_and_progress(client_, staff, tournament):
    client_.force_login(staff)
    html = client_.get('/admin?page=welcome').content.decode()
    assert 'Setup' in html
    # 16 players seeded, all with draw numbers → roster + draw ticks (the count
    # sits in its own <span>, so match the surrounding text, not the whole line).
    assert 'players imported' in html
    assert '>16<' in html
    assert 'Draw complete' in html
    # The live round-timer card is present.
    assert 'dashCounter' in html


def test_dashboard_empty_tenant_does_not_claim_all_rounds(client_, staff, tenant):
    """With no seats, the round-progress must read 0 — not nb_rounds — even though
    _last_complete_round returns nb_rounds when it finds no incomplete seat."""
    TournamentSettings.objects.create(tenant=tenant, welcome='W', nb_rounds=7)
    client_.force_login(staff)
    resp = client_.get('/admin?page=welcome')
    assert resp.status_code == 200
    html = resp.content.decode()
    assert 'No players yet' in html
    # complete_round rendered as "0 / 7", never "7 / 7".
    assert '/ 7' in html
    assert '>7 <' not in html
