"""Static checks on source and configuration — no database, no client.

These read project files off disk and assert properties of the source itself:
that a setting appears in one profile and no other, that a template can never
render an inescapable state. They catch bug classes no runtime test can see,
because the thing they guard is the absence of something.

Two rules for anything added here:

  - Resolve paths from ``REPO_ROOT``, never from the working directory, so the
    suite runs from anywhere.
  - Do not convert these into view tests. A test that renders a page cannot
    prove a setting is absent from a profile it never loads.
"""
import re

import pytest

from mahj.tests.conftest import REPO_ROOT


@pytest.mark.parametrize('profile', ['base.py', 'prod.py', 'standalone.py', 'dev.py'])
def test_fast_password_hasher_is_confined_to_the_test_profile(profile):
    """MD5 hashing is a test-only speedup; it must not reach a real profile.

    apps/settings/test.py pins PASSWORD_HASHERS to MD5 because production PBKDF2
    was 80% of the suite's wall time. That is safe only while every profile a
    real deployment loads leaves Django's default in place — so assert the
    setting is absent from all four of them, rather than trusting import order.
    """
    source = (REPO_ROOT / 'apps' / 'settings' / profile).read_text()
    assert 'PASSWORD_HASHERS' not in source, (
        f'apps/settings/{profile} sets PASSWORD_HASHERS. Only the test profile may, '
        'and only to speed up hashing — a real deployment must keep the Django default.'
    )


def test_no_access_decision_reads_the_staff_flag():
    """`docs/dev/access-control.md` reserves `is_staff` for the Django admin site
    and forbids keying any access decision on it: a tenant admin is a `Membership`
    row, and the admin site itself requires `is_superuser` (see
    `mahj/admin_site.py`). That leaves the flag granting nothing.

    Which is only true while nobody reintroduces a predicate on it. A stale
    `is_staff=True` on an old account is harmless today and a privilege leak the
    moment one comes back, and it comes back easily — it reads like the obvious
    "is this person staff" check. So this asserts the flag is never read outside
    the migration whose whole job was to convert it away.
    """
    offenders = []
    for root in ('mahj', 'apps'):
        for path in sorted((REPO_ROOT / root).rglob('*.py')):
            rel = path.relative_to(REPO_ROOT)
            # Migrations describe schema, not access decisions, and tests may
            # set the flag on fixtures to prove it is ignored.
            if 'migrations' in rel.parts or 'tests' in rel.parts:
                continue
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if '.is_staff' in line:
                    offenders.append(f'{rel}:{n}: {line.strip()}')
    assert not offenders, (
        'is_staff is read where an access decision is made. Use is_superuser for the '
        'platform operator, or a Membership role for a tenant:\n  '
        + '\n  '.join(offenders))


# ---------------------------------------------------------------------------
# The shared confirm dialog. The invariant lives in an Alpine expression, so the
# template source is the only place it can be checked — there is no browser here
# to click the button.
# ---------------------------------------------------------------------------

ADMIN_SHELL = REPO_ROOT / 'mahj' / 'templates' / 'mahj' / 'admin.html'
TEMPLATE_DIR = REPO_ROOT / 'mahj' / 'templates' / 'mahj'


class TestConfirmDialogIsAlwaysEscapable:
    """The shared modal can require typed confirmation, which disables its Confirm
    button until the text matches. A one-button notice (no Cancel) must never be
    subject to that — a disabled Confirm there traps the operator in the dialog with
    no way out, which is what happened when a failed action raised a notice while the
    modal still carried a prompt.
    """

    def _shell(self):
        return ADMIN_SHELL.read_text()

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
        offenders = []
        for path in sorted(TEMPLATE_DIR.glob('*.html')):
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
        body = self._shell().split('confirmBlocked() {')[1].split('},')[0]
        # The guard that makes it structural rather than incidental.
        assert 'if (m.hideCancel) return false;' in body

    def test_showalert_never_sets_a_prompt(self):
        alert_body = self._shell().split('showAlert(opts) {')[1].split('},')[0]
        assert "prompt: ''" in alert_body
        assert 'opts.prompt' not in alert_body

    def test_the_dialog_keeps_its_other_exits(self):
        """Escape and the backdrop, so even a blocked Confirm is not a dead end."""
        shell = self._shell()
        assert 'keydown.escape.window="confirmModal.open && closeConfirm()"' in shell
        assert 'bg-black/50 backdrop-blur-sm" @click="closeConfirm()"' in shell


class TestCacheInvalidationContract:
    """Every surface cached in views/scoring.py must be invalidated by signals.py.

    They agree only by convention — the prefixes are string literals in two files —
    and getting it wrong is silent: the surface just serves data up to SUB_CACHE_TTL
    stale, with nothing to notice. So the convention is checked rather than trusted.
    """

    def _prefixes(self):
        scoring = (REPO_ROOT / 'mahj' / 'views' / 'scoring.py').read_text()
        signals = (REPO_ROOT / 'mahj' / 'signals.py').read_text()
        written = set(re.findall(r"_cached\(\s*'([a-z_0-9]+)'", scoring))
        deleted = set(re.findall(
            r"cache\.delete\(f'([a-z_0-9]+):\{subdomain\}:\{full_view\}'\)", signals))
        return written, deleted

    def test_every_cached_surface_is_invalidated(self):
        written, deleted = self._prefixes()
        assert written, 'found no _cached() call sites — did the wrapper get renamed?'
        assert written - deleted == set(), (
            'these surfaces are cached but never invalidated: '
            f'{sorted(written - deleted)} — add them to signals.invalidate_leaderboard'
        )

    def test_signals_does_not_clear_surfaces_that_no_longer_exist(self):
        """The other direction, so the list doesn't rot into stale names."""
        written, deleted = self._prefixes()
        assert deleted - written == set(), (
            f'signals clears keys nothing writes: {sorted(deleted - written)}')


def test_x_forwarded_host_is_not_trusted_in_prod():
    """USE_X_FORWARDED_HOST would let a client pick the tenant with a header.
    Config, not code: nginx passes the real Host and never sets X-Forwarded-Host,
    so the setting must stay off. The behavioural half of this lives in
    test_membership.py's TestForwardedHostSpoofing.
    """
    prod = (REPO_ROOT / 'apps' / 'settings' / 'prod.py').read_text()
    assert 'USE_X_FORWARDED_HOST = True' not in prod


def test_no_test_reads_a_path_relative_to_the_working_directory():
    """The suite must run from any directory, so a test that opens a project file
    has to anchor it to REPO_ROOT. A bare relative path works only when pytest
    happens to be invoked from the repo root — and fails as a missing file, which
    reads like a broken test rather than a broken path.

    Cheaper to keep than to rediscover: this started as 5 source reads plus one
    relative *fixture* path, and that single fixture line failed 38 tests.
    """
    offenders = []
    for path in sorted((REPO_ROOT / 'mahj' / 'tests').rglob('*.py')):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"""(Path|open)\(\s*['"](mahj|apps|docs|scripts|standalone)/""", line):
                offenders.append(f'{path.name}:{n}: {line.strip()}')
    assert not offenders, (
        'these read a project path relative to the working directory; anchor them '
        'to conftest.REPO_ROOT:\n  ' + '\n  '.join(offenders))
