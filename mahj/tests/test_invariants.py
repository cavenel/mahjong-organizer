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
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


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
            # 0010_seed_memberships reads it to map retired global roles onto
            # Memberships. That is history and must keep working as written.
            if 'migrations' in rel.parts or 'tests' in rel.parts:
                continue
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if '.is_staff' in line:
                    offenders.append(f'{rel}:{n}: {line.strip()}')
    assert not offenders, (
        'is_staff is read where an access decision is made. Use is_superuser for the '
        'platform operator, or a Membership role for a tenant:\n  '
        + '\n  '.join(offenders))
