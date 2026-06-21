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

# Sessions are DB-backed in base.py, which suits tests too: the DummyCache here
# drops every write (so cache-invalidation assertions stay honest), and DB sessions
# are unaffected by that — force_login keeps working.

CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels.layers.InMemoryChannelLayer',
    }
}

MIDDLEWARE = [m for m in MIDDLEWARE if 'whitenoise' not in m.lower()]

# base.py uses manifest storage for cache-busting in prod, but that backend
# raises "Missing staticfiles manifest entry" when {% static %} is rendered
# without a collectstatic run. Tests don't run collectstatic, so fall back to
# the plain storage that resolves names directly.
STORAGES = {
    'staticfiles': {
        'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
    },
}
