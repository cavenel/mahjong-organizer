import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_MISSING = object()

def env(key, default=_MISSING):
    if default is _MISSING:
        return os.environ[key]  # raises KeyError loudly if unset
    return os.environ.get(key, default)

# The apex domain this instance is served under. Tenants live at
# <subdomain>.<BASE_DOMAIN>; it drives the CSRF trusted origins, the default
# prod ALLOWED_HOSTS, the advertised spectator-site URL, and the nginx vhost.
# Defaults to 'localhost' for local dev.
BASE_DOMAIN = env('BASE_DOMAIN', 'localhost')

INSTALLED_APPS = [
    'daphne',
    'channels',
    'mahj.apps.MahjConfig',
    # Not 'django.contrib.admin': the same app with the admin site narrowed to
    # superusers. See mahj/admin_site.py.
    'mahj.admin_site.MahjAdminConfig',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'mathfilters',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    # Logs a user in when a request carries a valid ?sesame=<token> (link-based
    # login for kiosk laptops). Must come after Django's AuthenticationMiddleware.
    'sesame.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'apps.middleware.AuthCookieMiddleware',
    # Turns a FieldError from the coercion helpers into a JSON 400 naming the
    # field. Last, so it sees exceptions from every view.
    'apps.middleware.FieldErrorMiddleware',
]

# Two Redis roles, deliberately split:
#   REDIS_URL      — the Django cache. Disposable + regenerable, so it runs with
#                    allkeys-lru: under memory pressure it sheds whatever's coldest.
#   REDIS_BUS_URL  — the Channels layer + the scan-OCR work queue. These must NOT
#                    be evicted: dropping a WebSocket group membership would
#                    silently strand a projector (the socket stays OPEN, so the
#                    client's reconnect/heartbeat never fires), and dropping a
#                    queued scan would lose a job. It runs on a separate noeviction
#                    Redis so the cache's LRU can't touch it.
# REDIS_BUS_URL defaults to REDIS_URL so single-instance dev/test is unchanged.
REDIS_URL = env('REDIS_URL', 'redis://127.0.0.1:6379/1')
REDIS_BUS_URL = env('REDIS_BUS_URL', REDIS_URL)

CACHES = {
    'default': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': REDIS_URL,
        'OPTIONS': {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
            # Every request path touches the cache — get_tenant, get_tournament, the
            # desktop HTML cache, the leaderboard generation counter, the scan
            # limiter. django_redis raises by default, so without this a Redis
            # restart or a full maxmemory on the noeviction bus doesn't degrade the
            # site, it 500s all of it at once: public standings, projector screens
            # and score entry. The cache is best-effort everywhere it is read — a
            # miss falls through to the database — so a failure must read as a miss.
            'IGNORE_EXCEPTIONS': True,
        },
    }
}
# Ignored, but not silently: paired with the `django`/`mahj` console loggers, a Redis
# problem still shows up in `docker logs web` instead of only as stale pages.
DJANGO_REDIS_IGNORE_EXCEPTIONS = True
DJANGO_REDIS_LOG_IGNORED_EXCEPTIONS = True

# Sessions live in the database, not the Redis cache: only staff (<=20) ever
# authenticate, so the per-request indexed lookup is negligible, and keeping auth
# off Redis means a Redis restart/flush/eviction can't log everyone out mid-event.
SESSION_ENGINE = 'django.contrib.sessions.backends.db'

ROOT_URLCONF = 'apps.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'mahj.context_processors.site_logo',
                'mahj.context_processors.public_site',
                'mahj.context_processors.role_flags',
            ],
        },
    },
]

AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
    'sesame.backends.ModelBackend',
]

# django-sesame: passwordless login links for scorer/display kiosk laptops.
# Tokens are STATELESS (signed, not stored), so validity is a single global TTL —
# there is no per-link expiry. The token mixes in the user's password hash, so
# rotating a user's password (set_unusable_password) invalidates ALL their links
# at once; that is how the User-management console "revokes" a user's links.
SESAME_MAX_AGE = int(env('SESAME_MAX_AGE', 30 * 24 * 3600))  # seconds; default 30 days

WSGI_APPLICATION = 'apps.wsgi.application'
ASGI_APPLICATION = 'apps.asgi.application'

CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            'hosts': [REDIS_BUS_URL],
            # A wedged/slow display socket would otherwise hold up to the default
            # 100 messages and keep them for 60s. Drop stale frames fast: a screen
            # that misses frames resyncs on its next heartbeat/reconnect anyway.
            'capacity': 300,
            'expiry': 10,
        },
    }
}

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': env('DB_NAME'),
        'USER': env('DB_USER'),
        'PASSWORD': env('DB_PASSWORD'),
        'HOST': env('DB_HOST', 'localhost'),
        'PORT': env('DB_PORT', '5432'),
        'CONN_MAX_AGE': 0,  # must be 0 in ASGI/UvicornWorker — persistent connections leak in async thread pools
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Wall-clock timezone of the venue, shown by the live clock on the projector
# standings screen. The server stores everything in UTC; this only affects what
# local time the display clock renders (IANA name, e.g. 'Europe/Stockholm').
VENUE_TZ = env('VENUE_TZ', 'UTC')

# Score-sheet camera scan / OCR (needs the Redis work queue + OpenCV/LLM
# scan_worker). The standalone single-binary profile turns this off — it ships
# without those heavy deps and enters scores manually.
SCAN_ENABLED = True

# Single-process sqlite build (see apps/settings/standalone.py). Read by the
# admin console to hide the pages a single-machine install has no use for.
STANDALONE = False

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
# Manifest storage hashes each file's name by its content (e.g.
# display_socket.a1b2c3.js) so {% static %} URLs change whenever a file
# changes. This is what makes nginx's "immutable, 1y" cache on /static/
# safe: clients (e.g. tablets) fetch the new URL instead of serving a stale
# cached copy under the old name.
STORAGES = {
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {'class': 'logging.StreamHandler'},
    },
    'loggers': {
        # Django's own logger, or a 500 goes nowhere. DEFAULT_LOGGING survives
        # (disable_existing_loggers is False), but its console handler carries
        # require_debug_true and its mail_admins handler needs ADMINS/EMAIL_*, none of
        # which are set anywhere — so under DEBUG=False an unhandled exception wrote
        # nothing at all to `docker logs web`. The mahj.* warnings that did appear came
        # from Python's last-resort handler, which is why this wasn't obvious.
        'django': {'handlers': ['console'], 'level': 'INFO'},
        # The app's own loggers, explicitly on the same handler rather than relying on
        # the last-resort one.
        'mahj': {'handlers': ['console'], 'level': 'INFO'},
        'mahj.views.scan': {'handlers': ['console'], 'level': 'INFO'},
        # Each projector screen holds a WebSocket and reconnects often, so the
        # per-connection "[accepted]" / "connection open" / "connection closed"
        # lines (all INFO) bury real errors once there are many screens. Keep
        # WARNING+ so genuine problems still surface.
        'uvicorn.error': {'level': 'WARNING'},
        'websockets': {'level': 'WARNING'},
    },
}

CSRF_TRUSTED_ORIGINS = [f'https://{BASE_DOMAIN}', f'https://*.{BASE_DOMAIN}']

# A home-screen app (Android) resumes a stale login page whose CSRF token no
# longer matches the rotated cookie; this handler redirects that failed login
# POST to a fresh login form instead of a dead-end 403. See apps/csrf.py.
CSRF_FAILURE_VIEW = 'apps.csrf.csrf_failure'
X_FRAME_OPTIONS = 'SAMEORIGIN'
