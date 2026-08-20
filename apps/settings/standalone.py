"""Standalone single-process profile: the same Django app, no external services.

Runs on a local sqlite file with an in-process cache and channel layer, so the
whole thing can be frozen into a PyInstaller binary and launched on a venue
laptop by a non-technical operator (see docs/hosting/STANDALONE.md and standalone/run.py).
Everything the robust Docker stack needs Postgres / pgbouncer / Redis / nginx /
workers for is either swapped for an in-process equivalent or dropped.
"""
import os

from django.core.management.utils import get_random_secret_key

# base.py resolves the Postgres DB_* env vars at import time (fails loud if
# unset). We override DATABASES to sqlite below, but base must still import — so
# satisfy those lookups with throwaway values first, mirroring test.py.
os.environ.setdefault('DB_NAME', 'standalone')
os.environ.setdefault('DB_USER', 'standalone')
os.environ.setdefault('DB_PASSWORD', 'standalone')

from .base import *  # noqa: E402,F401,F403

DEBUG = False

# The launcher generates and persists a secret key into the external .env on first
# run, so this is normally set. Fall back to an ephemeral key rather than refuse to
# boot — a laptop app hard-failing on startup is worse than a fresh key.
#
# What a fresh key costs, if the .env is lost or the app is started without the
# launcher: sessions and sesame login links are invalidated (harmless, log in
# again), and — less obviously — every stored publish-target password and private
# key becomes undecryptable, because publish/secrets.py derives its Fernet key from
# SECRET_KEY. The target has to be reconfigured. Keep the .env alongside the
# database when moving an install between machines.
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY') or get_random_secret_key()

# Bound to 0.0.0.0 so projector/scorer devices on the venue LAN can open screens
# by the laptop's LAN (or port-forwarded public) IP — which means the Host header
# is whatever address they used. Host validation isn't a security boundary for
# this single-tenant local app (the tenant is pinned via LOCAL_TENANT, not the
# host), so accept any Host rather than have LAN devices hit a 400.
ALLOWED_HOSTS = ['*']

# --- sqlite instead of Postgres ---------------------------------------------
# One local file, no pooling. The launcher points MAHJ_DB_PATH at a writable
# user-data dir (outside the read-only PyInstaller bundle). WAL + a busy timeout
# (set on every connection below) let the async Channels workers and the sync
# request threads share the file without "database is locked"; sqlite serializes
# writers, so concurrent writes wait rather than corrupt.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.environ.get('MAHJ_DB_PATH', os.path.join(BASE_DIR, 'mahj-standalone.sqlite3')),
        'OPTIONS': {'timeout': 10},
    }
}

# --- no Redis ----------------------------------------------------------------
# Per-process cache and channel layer. Single process, so this is complete: the
# desktop/modal HTML cache, leaderboard generation counter, and the projector
# WebSocket groups all live in this one process.
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
CHANNEL_LAYERS = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}

# --- serve static ourselves (no nginx) --------------------------------------
# Keep base.py's WhiteNoise middleware (prod strips it because nginx serves
# /static/). Use the non-manifest storage so a hashless collectstatic in the
# build stage is enough — no staticfiles.json lookup at runtime.
STORAGES = {
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage'},
}

# --- single tenant -----------------------------------------------------------
# This build serves one tournament. Pin every request to it (honoured by
# get_domain regardless of DEBUG, unlike the dev env-var path). Defaults to
# 'local', which the launcher also creates on first run.
LOCAL_TENANT = os.environ.get('LOCAL_TENANT', '').strip() or 'local'

# --- scan / OCR off ----------------------------------------------------------
# The binary ships without the Redis queue and OpenCV/LLM workers; scores are
# entered manually. Gates the scorer QR and 404s the scan endpoints.
SCAN_ENABLED = False

# --- single-process backup/restore ------------------------------------------
# Rolling sqlite snapshots + restore-on-relaunch, driven by the admin console's
# "Snapshot restore" page (standalone-only; see mahj/standalone_backup.py).
STANDALONE = True

# --- sqlite PRAGMAs on every connection --------------------------------------
# Django 4.2's sqlite backend doesn't run OPTIONS['init_command'], so set the
# durability/concurrency PRAGMAs via the connection_created signal. Guarded on
# vender=='sqlite' so it's inert if this module is ever combined with another DB.
from django.db.backends.signals import connection_created  # noqa: E402
from django.dispatch import receiver  # noqa: E402


@receiver(connection_created)
def _set_sqlite_pragmas(sender, connection, **kwargs):
    if connection.vendor == 'sqlite':
        cur = connection.cursor()
        cur.execute('PRAGMA journal_mode=WAL;')
        cur.execute('PRAGMA synchronous=NORMAL;')
        cur.execute('PRAGMA foreign_keys=ON;')
