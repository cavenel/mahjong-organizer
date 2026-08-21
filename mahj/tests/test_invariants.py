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
