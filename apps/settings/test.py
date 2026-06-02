import os

os.environ.setdefault('DB_NAME', 'test')
os.environ.setdefault('DB_USER', 'test')
os.environ.setdefault('DB_PASSWORD', 'test')

from .base import *

SECRET_KEY = 'test-insecure-key'
DEBUG = False
ALLOWED_HOSTS = ['*']

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.dummy.DummyCache',
    }
}

# base.py routes sessions through the cache, but tests use DummyCache (which drops
# every write) so cache-invalidation assertions stay honest. Cache-backed sessions
# would therefore fail on save (UpdateError) and break force_login. Use DB-backed
# sessions in tests instead — isolated from the DummyCache behaviour above.
SESSION_ENGINE = 'django.contrib.sessions.backends.db'

CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels.layers.InMemoryChannelLayer',
    }
}

MIDDLEWARE = [m for m in MIDDLEWARE if 'whitenoise' not in m.lower()]
