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

CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels.layers.InMemoryChannelLayer',
    }
}

MIDDLEWARE = [m for m in MIDDLEWARE if 'whitenoise' not in m.lower()]
