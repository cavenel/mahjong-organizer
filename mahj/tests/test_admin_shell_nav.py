"""Verification for the redesigned admin shell navigation (mahj/templates/mahj/admin.html).

Renders the real `options` view end-to-end (view + context + template) for each
role and asserts:
  * the page returns 200 (shell renders without error), and
  * role-based section visibility matches each role (staff sees every section;
    scorer/display_op/publisher see only their own).

Guards against the sidebar's `{% if %}` visibility guards drifting out of sync
with the server-side page gates in admin_views.py.
"""
import pytest
from django.contrib.auth.models import User
from django.test import Client

from mahj.tests.conftest import grant

HOST = 'test.example.com'


@pytest.fixture
def client_():
    c = Client()
    c.defaults['HTTP_HOST'] = HOST
    return c


def _role_user(username, tenant, **roles):
    u = User.objects.create_user(username, password='pw')
    grant(u, tenant, **roles)
    return u


@pytest.fixture
def staff(tournament):
    return _role_user('boss', tournament['tenant'], admin=True)


@pytest.fixture
def scorer(tournament):
    return _role_user('sc', tournament['tenant'], scorer=True)


@pytest.fixture
def display_op(tournament):
    return _role_user('op', tournament['tenant'], display_op=True)


@pytest.fixture
def publisher(tournament):
    return _role_user('pub', tournament['tenant'], publisher=True)


def _get_shell(client_, user):
    client_.force_login(user)
    resp = client_.get('/admin?page=welcome')
    assert resp.status_code == 200
    return resp.content.decode()


def test_staff_sees_every_section(client_, staff, tournament):
    html = _get_shell(client_, staff)
    for label in ('Configuration', 'Players', 'Scoring', 'Displays',
                  'Results', 'Administration'):
        assert f'>{label}</p>' in html, f'staff should see the {label} group'
    # Print / Export lifted out of the nav into the topbar dropdown.
    assert 'printMenuOpen=!printMenuOpen' in html   # the topbar button
    assert "showPrintModal('player_names')" in html
    # Accordion machinery is gone.
    assert 'toggleSection' not in html
    assert 'openSection' not in html
    # Icon-rail state is present.
    assert 'railCollapsed' in html


def test_scorer_sees_only_scoring(client_, scorer, tournament):
    html = _get_shell(client_, scorer)
    assert '>Scoring</p>' in html
    for hidden in ('>Configuration</p>', '>Players</p>', '>Displays</p>',
                   '>Results</p>', '>Administration</p>'):
        assert hidden not in html
    # Scorer is not a publisher: no publisher overview link.
    assert 'page=publisher_overview' not in html
    # Scorer can still reach the Scores print export.
    assert "showPrintModal('print_scores')" in html
    # ...but not the staff-only prepare exports.
    assert "showPrintModal('player_names')" not in html


def test_display_op_sees_only_displays(client_, display_op, tournament):
    html = _get_shell(client_, display_op)
    assert '>Displays</p>' in html
    assert 'page=display' in html and 'page=ceremony' in html
    for hidden in ('>Configuration</p>', '>Players</p>', '>Scoring</p>',
                   '>Results</p>', '>Administration</p>'):
        assert hidden not in html
    # Display op has no print exports → no print/export dropdown button.
    assert 'printMenuOpen=!printMenuOpen' not in html


def test_publisher_sees_scoring_and_overview(client_, publisher, tournament):
    html = _get_shell(client_, publisher)
    assert '>Scoring</p>' in html
    assert 'page=publisher_overview' in html
    for hidden in ('>Configuration</p>', '>Players</p>', '>Displays</p>',
                   '>Results</p>', '>Administration</p>'):
        assert hidden not in html


class TestConfirmDialogIsAlwaysEscapable:
    """The shared modal can require typed confirmation, which disables its Confirm
    button until the text matches. A one-button notice (no Cancel) must never be
    subject to that — a disabled Confirm there traps the operator in the dialog with
    no way out, which is what happened when a failed action raised a notice while the
    modal still carried a prompt.

    Asserted on the template because the invariant lives in an Alpine expression;
    there is no browser here to click it.
    """

    def _shell(self):
        import pathlib
        return pathlib.Path('mahj/templates/mahj/admin.html').read_text()

    def test_the_button_asks_one_shared_predicate(self):
        shell = self._shell()
        # Not an inline copy of the condition — the button, the Enter key and
        # runConfirm must all consult the same one or they can disagree.
        assert ':disabled="confirmBlocked()"' in shell
        assert 'if (this.confirmBlocked()) return;' in shell
        assert shell.count('confirmBlocked()') >= 3

    def test_the_predicate_returns_a_real_boolean(self):
        """Alpine removes a bound attribute only for null/undefined/false — anything
        else is *set*, and for a boolean attribute the value becomes the attribute
        name. An expression short-circuiting to '' therefore renders
        disabled="disabled". Every branch here has to yield an actual boolean."""
        body = self._shell().split('confirmBlocked() {')[1].split('},')[0]
        assert 'return false;' in body          # the hideCancel branch
        assert '!!m.prompt' in body             # coerced, not a bare string

    def test_no_disabled_binding_short_circuits_on_a_non_boolean(self):
        """The trap that broke every dialog: `:disabled="someString && …"`. A
        comparison as the left operand is fine — it yields a real false."""
        import pathlib
        import re
        offenders = []
        for path in pathlib.Path('mahj/templates/mahj').glob('*.html'):
            for expr in re.findall(r':disabled="([^"]*)"', path.read_text()):
                if '&&' not in expr:
                    continue
                left = expr.split('&&')[0].strip()
                # Safe if the left operand is itself a comparison or a call.
                if any(op in left for op in ('===', '!==', '==', '!=', '>=', '<=', '>', '<')):
                    continue
                if left.endswith(')') or left.startswith('!'):
                    continue
                offenders.append(f'{path.name}: {expr}')
        assert not offenders, (
            'these :disabled bindings can short-circuit to a non-boolean, which '
            f'Alpine renders as disabled="disabled": {offenders}')

    def test_a_one_button_notice_can_never_be_blocked(self):
        shell = self._shell()
        body = shell.split('confirmBlocked() {')[1].split('},')[0]
        # The guard that makes it structural rather than incidental.
        assert 'if (m.hideCancel) return false;' in body

    def test_showalert_never_sets_a_prompt(self):
        shell = self._shell()
        alert_body = shell.split('showAlert(opts) {')[1].split('},')[0]
        assert "prompt: ''" in alert_body
        assert 'opts.prompt' not in alert_body

    def test_the_dialog_keeps_its_other_exits(self):
        """Escape and the backdrop, so even a blocked Confirm is not a dead end."""
        shell = self._shell()
        assert 'keydown.escape.window="confirmModal.open && closeConfirm()"' in shell
        assert 'bg-black/50 backdrop-blur-sm" @click="closeConfirm()"' in shell
