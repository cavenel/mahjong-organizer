"""The cache must degrade, not 500.

Every request path reads the cache (get_tenant, get_tournament, the desktop HTML
cache, the leaderboard generation counter, the scan limiter), and django_redis raises
on a connection error by default. These point the real backend at a closed port, so
they exercise the configured behaviour rather than asserting a setting is present.
"""
import pytest
from django.core.cache import caches
from django.test import override_settings

# A port nothing listens on, so every operation fails to connect.
DEAD = 'redis://127.0.0.1:6389/0'


def _cache_settings(ignore):
    return {'default': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': DEAD,
        'OPTIONS': {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
            'IGNORE_EXCEPTIONS': ignore,
        },
    }}


def _fresh():
    """A cache handle built from the settings currently in force."""
    return caches.create_connection('default')


class TestUnreachableRedisReadsAsAMiss:
    def test_get_returns_none_instead_of_raising(self):
        with override_settings(CACHES=_cache_settings(True),
                               DJANGO_REDIS_IGNORE_EXCEPTIONS=True):
            c = _fresh()
            assert c.get('anything') is None

    def test_set_and_delete_do_not_raise(self):
        with override_settings(CACHES=_cache_settings(True),
                               DJANGO_REDIS_IGNORE_EXCEPTIONS=True):
            c = _fresh()
            c.set('k', 'v', 30)
            c.delete('k')
            assert c.get('k') is None

    def test_without_the_setting_it_really_does_raise(self):
        """The premise, pinned — otherwise the tests above would pass either way."""
        from redis.exceptions import ConnectionError as RedisConnectionError
        with override_settings(CACHES=_cache_settings(False),
                               DJANGO_REDIS_IGNORE_EXCEPTIONS=False):
            c = _fresh()
            with pytest.raises(RedisConnectionError):
                c.get('anything')


def test_the_project_configures_it():
    """Guard against the option being dropped from base.py in a later edit."""
    from apps.settings import base
    assert base.CACHES['default']['OPTIONS']['IGNORE_EXCEPTIONS'] is True
    assert base.DJANGO_REDIS_IGNORE_EXCEPTIONS is True
