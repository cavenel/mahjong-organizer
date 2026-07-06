"""Entry point for the standalone (PyInstaller) build.

Runs the same Django app as the Docker stack, single-process, on a local sqlite
file — for a venue laptop, launched by a non-technical operator. Ordered so
config is in place before Django loads:

  1. Load the external, user-editable .env (python-dotenv). Config is never baked
     into the binary; a first run writes a template and generates a secret key.
  2. Point the app at a writable sqlite file in the OS user-data dir.
  3. migrate, integrity-check, snapshot, bootstrap a tenant + admin user.
  4. Serve with uvicorn and open the browser.

Backups are the durability substitute for the Docker stack's Postgres: rolling
online-backup snapshots (safe on a live DB) into a snapshots/ dir, plus a
quick_check on startup. Recovery = quit, replace the .sqlite with a snapshot,
relaunch (see docs/STANDALONE.md).
"""
import os
import sys
import threading
import time
import warnings
import webbrowser
from pathlib import Path

# WhiteNoise serves /static/ via a sync-iterator streaming response; under ASGI
# Django emits a one-time warning as it falls back to sync_to_async. The files
# serve correctly — silence the noise so the app's console stays clean. (The
# cloud deployment serves static via nginx, so it never hits this path.)
warnings.filterwarnings(
    'ignore',
    message='StreamingHttpResponse must consume synchronous iterators.*',
)

HOST = '127.0.0.1'
PORT = 8000
SETTINGS_MODULE = 'apps.settings.standalone'
SNAPSHOT_INTERVAL_S = 5 * 60   # take a backup every 5 minutes while running

# .env keys that are meaningful in the standalone profile (the template written
# on first run). DB_* / Redis vars from the Docker profile don't apply here.
ENV_TEMPLATE = """\
# Mahjong standalone configuration. Edit, then restart the app.

# The tournament's tenant subdomain. Pins this laptop to one tenant.
LOCAL_TENANT=

# Venue wall-clock timezone (IANA name), for the projector clock.
VENUE_TZ=Europe/Stockholm

# Public site URL shown to spectators (projector QR + caption, printed cards).
# Set it to where you publish the static site below; leave blank to show nothing
# meaningful. Example: PUBLIC_SITE_URL=https://scores.example.org
PUBLIC_SITE_URL=

# --- Publish the public spectator site (optional) ---------------------------
# Leave PUBLISH_SFTP_HOST blank to disable web publishing.
PUBLISH_SFTP_HOST=
PUBLISH_SFTP_PORT=22
PUBLISH_SFTP_USER=
PUBLISH_SFTP_KEY=
PUBLISH_SFTP_PASSWORD=
PUBLISH_SFTP_PATH=
PUBLISH_TENANT=

# Auto-generated on first run — do not edit.
DJANGO_SECRET_KEY=
"""


def app_data_dir():
    """Writable per-user data dir for the sqlite DB, snapshots and .env."""
    if sys.platform == 'win32':
        base = os.environ.get('APPDATA') or str(Path.home())
    elif sys.platform == 'darwin':
        base = str(Path.home() / 'Library' / 'Application Support')
    else:
        base = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    d = Path(base) / 'Mahjong'
    d.mkdir(parents=True, exist_ok=True)
    return d


def binary_dir():
    """Directory the app was launched from (next to the .exe/.app when frozen)."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def load_config(data_dir):
    """Load .env from next-to-binary or the user-data dir; create it on first run.

    Returns the Path of the file in use. Generates and persists a secret key if
    the file has none.
    """
    from dotenv import load_dotenv, set_key

    candidates = [binary_dir() / '.env', data_dir / '.env']
    env_path = next((p for p in candidates if p.exists()), None)
    if env_path is None:
        env_path = data_dir / '.env'
        env_path.write_text(ENV_TEMPLATE, encoding='utf-8')
        print(f"First run: created a config file at {env_path}\n"
              f"Fill in your settings (tenant, web publishing) and restart.")

    load_dotenv(env_path)

    if not os.environ.get('DJANGO_SECRET_KEY'):
        from django.core.management.utils import get_random_secret_key
        key = get_random_secret_key()
        set_key(str(env_path), 'DJANGO_SECRET_KEY', key)
        os.environ['DJANGO_SECRET_KEY'] = key
    return env_path


def db_path(data_dir):
    return str(data_dir / 'mahj.sqlite3')


def _snapshot_loop():
    # Snapshot + integrity logic lives in mahj.standalone_backup so the admin
    # restore page and this launcher share one implementation.
    from mahj import standalone_backup
    while True:
        time.sleep(SNAPSHOT_INTERVAL_S)
        try:
            standalone_backup.take_snapshot()
        except Exception as e:
            print(f"Snapshot failed: {e}")


def bootstrap():
    """First-run data: a Tenant (from LOCAL_TENANT) and an admin user."""
    from django.contrib.auth.models import User
    from mahj.models import Tenant

    subdomain = os.environ.get('LOCAL_TENANT', '').strip() or 'local'
    Tenant.objects.get_or_create(subdomain=subdomain, defaults={'name': subdomain})
    if not User.objects.filter(is_superuser=True).exists():
        User.objects.create_superuser('admin', '', 'admin')
        print("Created admin user  (username: admin  password: admin)  — change it in the app.")


def main():
    os.environ['DJANGO_SETTINGS_MODULE'] = SETTINGS_MODULE
    data_dir = app_data_dir()
    load_config(data_dir)

    path = db_path(data_dir)
    os.environ['MAHJ_DB_PATH'] = path

    # Shared with the admin "Database restore" page. Imported here (after
    # MAHJ_DB_PATH is set, before django.setup) because it must swap the sqlite
    # file before anything opens it.
    from mahj import standalone_backup

    # Apply a restore the admin scheduled last session (they picked a snapshot;
    # a single process can't swap its own open DB, so it's applied on relaunch).
    restored = standalone_backup.apply_pending_restore()
    if restored:
        print(f"Restored database from snapshot: {restored}")

    if not standalone_backup.integrity_ok(path):
        print("\n*** The database failed its integrity check. ***\n"
              f"Its file is: {path}\n"
              f"Restore the newest snapshot from {standalone_backup.snapshots_dir()} "
              "(rename it to mahj.sqlite3) and relaunch.\n")
        sys.exit(1)

    import django
    django.setup()
    from django.core.management import call_command
    call_command('migrate', '--noinput', verbosity=0)
    bootstrap()

    standalone_backup.take_snapshot()  # known-good snapshot at boot
    threading.Thread(target=_snapshot_loop, daemon=True).start()

    url = f'http://{HOST}:{PORT}/options'
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"\nMahjong admin is running at {url}\n(Close this window to stop.)\n")

    import uvicorn
    uvicorn.run('apps.asgi:application', host=HOST, port=PORT, log_level='warning')


if __name__ == '__main__':
    main()
