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
from django.contrib.auth.models import User
from django.test import Client

from mahj.models import Schedule, Screen, ScreenMode, TournamentSettings
from mahj.views.admin_views import _mode_breakdowns, _pretty_view
from mahj.tests.conftest import grant

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
    # The teams density is a real screen view; it used to be labelled "detailed",
    # disagreeing with the same function's JS twin on the display admin page.
    ('scores:teams:all', 'Standings — teams, all (rotating)'),
    ('scores:teams:3', 'Standings — teams, page 3'),
    # An unknown density still falls back to detailed.
    ('scores:nonsense:all', 'Standings — detailed, all (rotating)'),
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
def display_op(tournament):
    u = User.objects.create_user('op', password='pw')
    grant(u, tournament['tenant'], display_op=True)
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
        '/admin?page=display&action=set_tournament&tournament-welcome=Lunch%20now%0ABack%20at%2014%3A00')

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

    # A GET can't mutate — the rename is POST-only.
    assert client_.get(f'/update_screen_name?id={screen.id}&name=X').status_code == 405

    resp = client_.post(f'/update_screen_name?id={screen.id}&name=Main+hall')
    assert resp.status_code == 200
    screen.refresh_from_db()
    assert screen.name == 'Main hall'

    # The renamed screen's label appears on the admin page as "/N — Name".
    html = client_.get('/admin?page=display').content.decode()
    assert 'Main hall' in html

    # An empty name clears it back to the bare positional label.
    client_.post(f'/update_screen_name?id={screen.id}&name=')
    screen.refresh_from_db()
    assert screen.friendly_name == ''


# ── Mutations are POST-only (no state change via a GET link) ──────────────────

def test_display_actions_reject_get(client_, display_op, tournament):
    """The screen/mode actions mutate state, so a GET must be refused (405) —
    a GET link would be a working CSRF vector against a logged-in operator."""
    client_.force_login(display_op)
    for url in ('/admin?page=display&action=add_screen',
                '/admin?page=display&action=remove_screen',
                '/admin?page=display&action=set_all_views&view=black'):
        assert client_.get(url).status_code == 405, url


def test_add_and_remove_screen_via_post(client_, display_op, tournament):
    tenant = tournament['tenant']
    client_.force_login(display_op)
    before = Screen.objects.filter(tenant=tenant).count()

    assert client_.post('/admin?page=display&action=add_screen').status_code in (200, 302)
    assert Screen.objects.filter(tenant=tenant).count() == before + 1

    assert client_.post('/admin?page=display&action=remove_screen').status_code in (200, 302)
    assert Screen.objects.filter(tenant=tenant).count() == before


def test_remove_screen_with_none_present_does_not_500(client_, display_op, tournament):
    """Removing when there are no screens is a no-op, not a 500."""
    tenant = tournament['tenant']
    Screen.objects.filter(tenant=tenant).delete()
    client_.force_login(display_op)
    assert client_.post('/admin?page=display&action=remove_screen').status_code in (200, 302)


def test_rm_mode_bad_id_is_404(client_, display_op, tournament):
    client_.force_login(display_op)
    assert client_.post('/admin?page=display&rm_mode=999999').status_code == 404
    assert client_.post('/admin?page=display&rm_mode=abc').status_code == 404


def test_display_op_cannot_set_structural_fields(client_, display_op, tournament):
    """The display page's set_tournament is allowlisted to layout fields, so a
    display operator can't reach structural settings (nb_rounds/rules/has_teams)."""
    tenant = tournament['tenant']
    before = TournamentSettings.objects.get(tenant=tenant).nb_rounds
    client_.force_login(display_op)

    resp = client_.post(
        f'/admin?page=display&action=set_tournament&tournament-nb_rounds={before + 5}')
    assert resp.status_code == 200
    assert TournamentSettings.objects.get(tenant=tenant).nb_rounds == before


def test_admin_logout_requires_post(client_, display_op, tournament):
    client_.force_login(display_op)
    # A GET link must not log the operator out (CSRF-able navigation).
    assert client_.get('/admin?logout=1').status_code == 405
    assert client_.get('/admin?page=display').status_code == 200  # still logged in
    # POST logs out; a subsequent admin hit redirects to login.
    assert client_.post('/admin?logout=1').status_code == 302
    assert client_.get('/admin').status_code == 302


def test_public_logout_requires_post(client_, display_op, tournament):
    client_.force_login(display_op)
    assert client_.get('/?logout=1').status_code == 405
    assert client_.post('/?logout=1').status_code == 302  # logged out, back to /


# ── set_tournament error surfacing ────────────────────────────────────────────
# A save that can't fit the DB used to bubble up as a bare 500 the admin page
# swallowed silently. set_tournament now validates length up front and returns a
# readable 400, which the page shows in an alert dialog.

def test_set_tournament_rejects_over_long_message(client_, display_op, tournament):
    tenant = tournament['tenant']
    TournamentSettings.objects.filter(tenant=tenant).update(welcome='ok')
    client_.force_login(display_op)

    too_long = 'x' * 300  # welcome is max_length=255
    resp = client_.post(
        f'/admin?page=display&action=set_tournament&tournament-welcome={too_long}',
        {'csrfmiddlewaretoken': 'x'})

    assert resp.status_code == 400
    body = resp.content.decode()
    assert 'On-screen message is too long' in body
    assert '255' in body
    # The rejected value was not persisted.
    assert TournamentSettings.objects.get(tenant=tenant).welcome == 'ok'


def test_set_tournament_saves_valid_message(client_, display_op, tournament):
    tenant = tournament['tenant']
    client_.force_login(display_op)

    resp = client_.post(
        '/admin?page=display&action=set_tournament&tournament-welcome=Round+3+starts+soon',
        {'csrfmiddlewaretoken': 'x'})

    assert resp.status_code == 200
    assert TournamentSettings.objects.get(tenant=tenant).welcome == 'Round 3 starts soon'


# ── Tournament settings page (admin?page=settings) ───────────────────────────
# Staff-only page exposing tournament identity (title/full name/city/period/
# rules), round count/length and the logo. The shared set_tournament handler
# persists edits regardless of which page posts them.

@pytest.fixture
def staff(tenant):
    # Depend on the bare `tenant` (not `tournament`) so the empty-tenant dashboard
    # test isn't handed a seeded player list; tournament-based tests share this tenant.
    u = User.objects.create_user('boss', password='pw')
    grant(u, tenant, admin=True)
    return u


def test_settings_page_renders_identity_fields_for_staff(client_, staff, tournament):
    client_.force_login(staff)
    html = client_.get('/admin?page=settings').content.decode()
    assert 'Tournament settings' in html
    assert 'tournament-title' in html
    assert 'tournament-nb_rounds' in html
    # Round length + logo are staff surfaces, shown here.
    assert 'tournament-total_time' in html


def test_settings_page_forbidden_for_non_staff(client_, display_op, tournament):
    """A display op reaching ?page=settings must get nothing (nav hides it, but
    the route has to enforce it too)."""
    client_.force_login(display_op)
    html = client_.get('/admin?page=settings').content.decode()
    assert 'tournament-title' not in html


def test_settings_page_saves_identity_via_set_tournament(client_, staff, tournament):
    tenant = tournament['tenant']
    client_.force_login(staff)
    resp = client_.post(
        '/admin?page=settings&action=set_tournament&tournament-city=Uppsala&tournament-nb_rounds=9',
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
# Staff-only inline table for correcting player metadata. Fields are read-only until
# the Edit players button unlocks them, then each one autosaves on change;
# draw_number is editable here too and goes through admin_player_draw_assign.

def test_player_editor_renders_players_for_staff(client_, staff, tournament):
    client_.force_login(staff)
    html = client_.get('/admin?page=player_editor').content.decode()
    assert 'Edit players' in html
    assert 'playerEditor()' in html
    assert 'Player1 Lastname' in html
    # Draw number is now an editable column backed by the valid-slot list.
    assert 'Draw number' in html
    assert 'valid-draw-data' in html
    assert 'changeDraw(' in html
    # Fields start locked; an Edit players button unlocks them.
    assert 'toggleEdit()' in html
    assert ':readonly="!editing"' in html


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
            'id': p.id, 'first_name': 'Corrected', 'last_name': 'Name',
            'country': 'Norway', 'EMA_ID': '99999', 'team': 'Reds',
        }]}),
        content_type='application/json')
    assert resp.status_code == 200
    p.refresh_from_db()
    assert p.country == 'Norway'
    # The editor edits first/last; full_name and short_name are recomputed from them.
    assert (p.first_name, p.last_name) == ('Corrected', 'Name')
    assert p.full_name == 'Corrected Name'
    assert p.short_name == 'Corrected'
    # Stored in the importer's canonical zero-padded form, so an edited id matches
    # the same competitor imported from a template.
    assert p.EMA_ID == '00099999'


def test_player_editor_save_rejects_a_non_numeric_ema_id(client_, staff, tournament):
    """The editor used to accept any string, and the template export then died on
    int() over it."""
    from mahj.models import Player
    p = Player.objects.filter(tenant=tournament['tenant']).first()
    before = p.EMA_ID
    client_.force_login(staff)
    resp = client_.post(
        '/player_editor_save',
        data=json.dumps({'players': [{'id': p.id, 'EMA_ID': 'N/A'}]}),
        content_type='application/json')
    assert resp.status_code == 400
    p.refresh_from_db()
    assert p.EMA_ID == before


def test_player_editor_save_accepts_a_blank_ema_id(client_, staff, tournament):
    """Most competitors have no EMA number, so blank has to stay allowed."""
    from mahj.models import Player
    p = Player.objects.filter(tenant=tournament['tenant']).first()
    client_.force_login(staff)
    resp = client_.post(
        '/player_editor_save',
        data=json.dumps({'players': [{'id': p.id, 'EMA_ID': ''}]}),
        content_type='application/json')
    assert resp.status_code == 200
    p.refresh_from_db()
    assert p.EMA_ID == ''


def test_template_export_survives_a_legacy_non_numeric_ema_id(client_, staff, tournament):
    """Rows predating the digits-only rule still exist (the shared fixture seeds
    them), and the export used to 500 on int() over one."""
    from mahj.models import Player
    Player.objects.filter(tenant=tournament['tenant']).update(EMA_ID='E00001')
    client_.force_login(staff)
    resp = client_.get('/admin_export_to_template')
    assert resp.status_code == 200


def test_player_editor_save_ignores_unknown_ids(client_, staff, tournament):
    client_.force_login(staff)
    resp = client_.post(
        '/player_editor_save',
        data=json.dumps({'players': [{'id': 999999, 'full_name': 'Ghost'}]}),
        content_type='application/json')
    assert resp.status_code == 200


@pytest.mark.parametrize('row', [
    {'id': 'x', 'country': 'France'},
    {'id': None, 'country': 'France'},
    {'id': [], 'country': 'France'},
    {'country': 'France'},
])
def test_player_editor_save_rejects_an_unreadable_id(client_, staff, tournament, row):
    """An unreadable id names no competitor, so the batch is refused rather than
    part-applied. It used to be a 500 out of the `id__in` lookup."""
    client_.force_login(staff)
    resp = client_.post(
        '/player_editor_save', data=json.dumps({'players': [row]}),
        content_type='application/json')
    assert resp.status_code == 400
    assert b'<html' not in resp.content.lower()


def test_player_editor_save_accepts_a_numeric_string_id(client_, staff, tournament):
    """A numeric string used to pass the lookup and then miss the int-keyed row
    map — the edit was silently dropped while the editor reported it saved."""
    client_.force_login(staff)
    player = tournament['players'][0]
    resp = client_.post(
        '/player_editor_save',
        data=json.dumps({'players': [{'id': str(player.id), 'country': 'Iceland'}]}),
        content_type='application/json')
    assert resp.status_code == 200, resp.content
    player.refresh_from_db()
    assert player.country == 'Iceland'


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
    # 16 players seeded, all with draw numbers → player list + draw ticks (the count
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


# --------------------------------------------------------------------------
# Actions land the operator back on the panel they just changed
# --------------------------------------------------------------------------

def _details_is_open(html, element_id):
    import re
    m = re.search(rf'<details id="{element_id}"[^>]*>', html)
    assert m, f'no <details id="{element_id}"> in the page'
    return m.group(0).endswith(' open>')


class TestPanelReopensAfterAScreenChange:
    """Adding or removing a screen reloads the page. The screen grid lives in a
    collapsed <details>, so without this the operator is dropped at the top of a long
    page with the thing they just changed folded shut.

    Carried as a query parameter rather than a URL fragment: a fragment never reaches
    the server, so the panel could only be reopened by JS after it had already
    rendered shut.
    """

    def _post(self, client, action):
        return client.post(f'/admin?page=display&action={action}')

    def test_add_screen_redirects_with_the_panel_flag(self, client_, staff, tournament):
        client_.force_login(staff)
        resp = self._post(client_, 'add_screen')
        assert resp.status_code == 302
        assert resp['Location'] == 'admin?page=display&open=screens'

    def test_remove_screen_redirects_with_it_too(self, client_, staff, tournament):
        client_.force_login(staff)
        Screen.objects.create(tenant=tournament['tenant'], name='S1', view='black')
        resp = self._post(client_, 'remove_screen')
        assert resp.status_code == 302
        assert resp['Location'] == 'admin?page=display&open=screens'

    def test_add_mode_redirects_with_it_too(self, client_, staff, tournament):
        client_.force_login(staff)
        Screen.objects.create(tenant=tournament['tenant'], name='S1', view='black')
        resp = client_.post('/admin?page=display&action=add_mode', {'mode_name': 'M'})
        assert resp.status_code == 302
        assert resp['Location'] == 'admin?page=display&open=screens'

    def test_the_flag_renders_the_panel_open(self, client_, staff, tournament):
        client_.force_login(staff)
        Screen.objects.create(tenant=tournament['tenant'], name='S1', view='black')
        html = client_.get('/admin?page=display&open=screens').content.decode()
        assert _details_is_open(html, 'configure-screens')
        assert not _details_is_open(html, 'display-settings')

    def test_the_settings_panel_has_its_own_flag(self, client_, staff, tournament):
        client_.force_login(staff)
        Screen.objects.create(tenant=tournament['tenant'], name='S1', view='black')
        html = client_.get('/admin?page=display&open=settings').content.decode()
        assert _details_is_open(html, 'display-settings')
        assert not _details_is_open(html, 'configure-screens')

    def test_a_plain_visit_leaves_both_collapsed(self, client_, staff, tournament):
        """The panels are collapsed by design — this must not open them for everyone."""
        client_.force_login(staff)
        Screen.objects.create(tenant=tournament['tenant'], name='S1', view='black')
        html = client_.get('/admin?page=display').content.decode()
        assert not _details_is_open(html, 'configure-screens')
        assert not _details_is_open(html, 'display-settings')

    def test_an_unknown_flag_opens_nothing(self, client_, staff, tournament):
        client_.force_login(staff)
        Screen.objects.create(tenant=tournament['tenant'], name='S1', view='black')
        html = client_.get('/admin?page=display&open=../../etc/passwd').content.decode()
        assert not _details_is_open(html, 'configure-screens')
        assert not _details_is_open(html, 'display-settings')


class TestSettingsAutosaveIsVisible:
    """The tournament fields autosave on change. They used to do it silently — only a
    failure spoke up — so an operator had no way to tell an edit had landed short of
    reloading. The Schedule card on the same page always reported itself, which made
    the page inconsistent with itself as well as unhelpful."""

    def test_every_field_card_carries_its_own_status_slot(self, client_, staff, tournament):
        """Reported in the card being edited, not once at the top: Format and Rounds
        sit below the fold, where a page-level status is invisible exactly when it is
        wanted."""
        import re
        body = None
        client_.force_login(staff)
        body = client_.get('/admin?page=settings').content.decode()
        cards = re.findall(r'class="settings-card\b', body)
        slots = re.findall(r'class="settings-save-state\b', body)
        assert len(cards) == 3, f'expected Identity/Format/Rounds, found {len(cards)}'
        assert len(slots) == len(cards), 'every card needs exactly one status slot'

    def test_every_autosaving_field_sits_in_a_card(self, client_, staff, tournament):
        """A field outside a .settings-card would report into the wrong slot."""
        import re
        client_.force_login(staff)
        body = client_.get('/admin?page=settings').content.decode()
        # Split on the card boundaries; no tournament-input may appear before the first.
        head = body.split('class="settings-card')[0]
        assert 'tournament-input' not in head

    def test_the_autosave_reports_success_and_failure(self, client_, staff, tournament):
        client_.force_login(staff)
        body = client_.get('/admin?page=settings').content.decode()
        # Same wording and fade as the Schedule card, so the page reads as one thing.
        assert "settingsSaveState('Saving…', $card)" in body
        assert "settingsSaveState('Saved', $card)" in body
        assert "settingsSaveState('', $card)" in body

    def test_a_field_still_saves(self, client_, staff, tournament):
        """The indicator must not have disturbed the save it reports on."""
        client_.force_login(staff)
        resp = client_.post(
            '/admin?page=settings&action=set_tournament&tournament-city=Uppsala')
        assert resp.status_code in (200, 302)
        tournament['settings'].refresh_from_db()
        assert tournament['settings'].city == 'Uppsala'

    def test_an_over_long_field_still_reports_the_reason(self, client_, staff, tournament):
        client_.force_login(staff)
        resp = client_.post(
            '/admin?page=settings&action=set_tournament&tournament-city=' + 'x' * 200)
        assert resp.status_code == 400
        assert b'too long' in resp.content


class TestScheduleReordering:
    """The agenda's order is meaningful — the Nth round-row becomes Round N, and the
    Schedule screen and printed handouts follow it — so the editor needs a way to
    change it, and the save has to persist it."""

    def test_the_editor_offers_move_up_and_down(self, client_, staff, tournament):
        client_.force_login(staff)
        body = client_.get('/admin?page=settings').content.decode()
        assert 'moveRow(i, -1)' in body
        assert 'moveRow(i, 1)' in body
        # Disabled at the ends, via comparisons so the binding yields a real boolean.
        assert ':disabled="i === 0"' in body
        assert ':disabled="i === rows.length - 1"' in body

    def test_saving_a_reordered_agenda_keeps_the_new_order(self, client_, staff, tournament):
        import json as _json
        from mahj.models import Schedule
        client_.force_login(staff)
        tenant = tournament['tenant']
        # Post the fixture's agenda with the first two rounds swapped.
        rows = [{'day': 'Sat', 'time': '11:00', 'name': 'Round 2', 'is_round': True},
                {'day': 'Sat', 'time': '10:00', 'name': 'Round 1', 'is_round': True},
                {'day': 'Sat', 'time': '12:00', 'name': 'Round 3', 'is_round': True}]
        resp = client_.post('/admin?page=settings&action=save_schedule',
                            {'schedule': _json.dumps(rows)})
        assert resp.status_code == 200
        stored = list(Schedule.objects.filter(tenant=tenant)
                      .order_by('id').values_list('name', flat=True))
        assert stored == ['Round 2', 'Round 1', 'Round 3']

    def test_the_round_mapping_follows_the_new_order(self, client_, staff, tournament):
        """The point of the reorder: round N is the Nth round-row, so moving a row
        moves which agenda entry drives which playing round."""
        import json as _json
        from mahj.scoring import player_schedule
        client_.force_login(staff)
        rows = [{'day': 'Sun', 'time': '09:00', 'name': 'Early round', 'is_round': True},
                {'day': 'Sun', 'time': '12:00', 'name': 'Lunch', 'is_round': False},
                {'day': 'Sun', 'time': '13:00', 'name': 'Late round', 'is_round': True}]
        client_.post('/admin?page=settings&action=save_schedule',
                     {'schedule': _json.dumps(rows)})
        rounds = [r for r in player_schedule(tournament['tenant']) if r.is_round]
        assert [r.name for r in rounds] == ['Early round', 'Late round']
