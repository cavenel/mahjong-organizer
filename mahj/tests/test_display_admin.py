"""Display admin page (admin?page=display): the mode-breakdown helpers that let
each saved mode show what it puts on every screen, plus the page rendering its
active-mode highlight.

A ScreenMode stores `views` as a JSON list of view strings in screen order; the
admin shows that breakdown and marks the mode whose views match the screens'
current views as active.
"""
import types

import pytest
import json
from django.contrib.auth.models import Group, User
from django.test import Client

from mahj.models import Screen, ScreenMode, Variable
from mahj.views.admin_views import _mode_breakdowns, _pretty_view

HOST = 'test.mahj.ovh'


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
        _mode(1, 'Tournament', json.dumps(['scores:detailed:all', 'counter'])),
        _mode(2, 'Break', json.dumps(['black', 'black'])),
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
    modes = [_mode(1, 'Off', json.dumps(['black', 'black']))]
    out = _mode_breakdowns(modes, screens)

    assert out[0]['is_active'] is True
    assert [r['pretty'] for r in out[0]['rows']] == ['Blank', 'Blank']


def test_mode_breakdown_fewer_views_than_screens():
    """A mode saved before a 4th screen was added: applying it leaves the surplus
    screen untouched, so the row reads 'unchanged' and the mode still counts as
    active when its covered screens match (matching how zip() applies it)."""
    screens = [_screen('scores:detailed:all'), _screen('counter'),
               _screen('schedule'), _screen('black')]
    modes = [_mode(1, 'ThreeOfFour',
                   json.dumps(['scores:detailed:all', 'counter', 'schedule']))]
    out = _mode_breakdowns(modes, screens)[0]

    assert out['is_active'] is True
    assert len(out['rows']) == 4
    assert out['rows'][3] == {'label': '/4', 'pretty': 'unchanged', 'unchanged': True}


def test_mode_breakdown_covered_screen_differs_is_not_active():
    """If a screen the mode controls doesn't match, the mode isn't active."""
    screens = [_screen('counter'), _screen('schedule')]
    modes = [_mode(1, 'M', json.dumps(['counter', 'black']))]
    assert _mode_breakdowns(modes, screens)[0]['is_active'] is False


def test_mode_breakdown_more_views_than_screens():
    """A mode saved with more screens than now exist: surplus views are dropped,
    and it's active when the remaining screens match."""
    screens = [_screen('counter')]
    modes = [_mode(1, 'Two', json.dumps(['counter', 'schedule']))]
    out = _mode_breakdowns(modes, screens)[0]

    assert out['is_active'] is True
    assert len(out['rows']) == 1  # only as many rows as there are screens


def test_mode_breakdown_handles_malformed_views():
    """Malformed JSON degrades to an empty mode: it covers no screens (every row
    reads 'unchanged') and is never active."""
    screens = [_screen('counter')]
    modes = [_mode(1, 'Broken', 'not valid json')]
    out = _mode_breakdowns(modes, screens)

    assert [r['unchanged'] for r in out[0]['rows']] == [True]
    assert out[0]['is_active'] is False
    assert out[0]['views_json'] == '[]'


def test_mode_breakdown_views_json_is_compact():
    """views_json must match JS JSON.stringify() byte-for-byte (no spaces) so the
    client-side active-mode comparison works."""
    screens = [_screen('scores:detailed:all'), _screen('counter')]
    modes = [_mode(1, 'T', json.dumps(['scores:detailed:all', 'counter']))]
    assert _mode_breakdowns(modes, screens)[0]['views_json'] == \
        '["scores:detailed:all","counter"]'


def test_mode_breakdown_label_includes_friendly_name():
    """A renamed screen appends its name to the positional endpoint label."""
    screens = [_screen('counter', friendly_name='Main hall'), _screen('black')]
    modes = [_mode(1, 'T', json.dumps(['counter', 'black']))]
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
        views=json.dumps(['scores:detailed:all', 'counter']))
    ScreenMode.objects.create(
        tenant=tenant, name='Break', views=json.dumps(['black', 'black']))

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
    ScreenMode.objects.create(tenant=tenant, name='Break', views=json.dumps(['black']))

    client_.force_login(display_op)
    html = client_.get('/admin?page=display').content.decode()

    assert 'mode-card mode-card--active' not in html


# ── Add mode ──────────────────────────────────────────────────────────────────

def test_views_field_is_unbounded():
    # add_mode stores json.dumps of every screen's view, which grows past any
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
    assert json.loads(mode.views) == views
    assert len(mode.views) > 100


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
    Variable.objects.filter(tenant=tenant).update(welcome='ok')
    client_.force_login(display_op)

    too_long = 'x' * 300  # welcome is max_length=255
    resp = client_.post(
        f'/admin?page=display&action=set_variable&variables-welcome={too_long}',
        {'csrfmiddlewaretoken': 'x'})

    assert resp.status_code == 400
    body = resp.content.decode()
    assert 'Counter message is too long' in body
    assert '255' in body
    # The rejected value was not persisted.
    assert Variable.objects.get(tenant=tenant).welcome == 'ok'


def test_set_variable_saves_valid_message(client_, display_op, tournament):
    tenant = tournament['tenant']
    client_.force_login(display_op)

    resp = client_.post(
        '/admin?page=display&action=set_variable&variables-welcome=Round+3+starts+soon',
        {'csrfmiddlewaretoken': 'x'})

    assert resp.status_code == 200
    assert Variable.objects.get(tenant=tenant).welcome == 'Round 3 starts soon'
