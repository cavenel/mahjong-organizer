"""Entry point for the standalone (PyInstaller) build.

Runs the same Django app as the Docker stack, single-process, on a local sqlite
file — for a venue laptop, launched by a non-technical operator. Ordered so
config is in place before Django loads:

  1. Load the external, user-editable .env (python-dotenv). Config is never baked
     into the binary; a first run writes a template and generates a secret key.
  2. Point the app at a writable sqlite file in the OS user-data dir.
  3. migrate, integrity-check, bootstrap a tenant + admin user.
  4. Serve with uvicorn and open the browser.

Durability is a quick_check on startup plus tournament dumps the operator
downloads manually (Administration -> Backup & restore) — there is no automatic
backup. Recovery = quit, delete the .sqlite file, relaunch (a fresh database is
created), then restore a downloaded dump in the console
(see docs/hosting/standalone.md).
"""
import os
import secrets
import sqlite3
import stat
import sys
import threading
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

# Bind all interfaces so projector/scorer devices on the venue LAN can reach the
# screens by the laptop's LAN IP; the operator's own browser still uses loopback.
BIND_HOST = '0.0.0.0'
OPEN_HOST = '127.0.0.1'
PORT = 8000
SETTINGS_MODULE = 'apps.settings.standalone'

# .env keys that are meaningful in the standalone profile (the template written
# on first run). DB_* / Redis vars from the Docker profile don't apply here.
ENV_TEMPLATE = """\
# Mahjong standalone configuration. Edit, then restart the app.

# Venue wall-clock timezone (IANA name), for the projector clock.
VENUE_TZ=Europe/Stockholm

# To publish the public spectator site to a web host, configure a target in the
# admin console (Administration → Publish target) — no env vars needed. The
# spectator URL advertised on screens/cards is set there too (Public URL).

# Auto-generated on first run — do not edit.
DJANGO_SECRET_KEY=
"""


def app_data_dir():
    """Writable per-user data dir for the sqlite DB and .env."""
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


def integrity_ok(path):
    """PRAGMA quick_check the sqlite file. True if healthy or absent.

    The launcher refuses to serve a database that fails this, rather than
    starting on one that half-works and renders wrong standings. Recovery is to
    delete the file and restore a tournament dump into the fresh database the
    next launch creates — see docs/hosting/standalone.md.
    """
    p = Path(path)
    if not p.exists():
        return True
    try:
        con = sqlite3.connect(str(p))
        try:
            row = con.execute('PRAGMA quick_check;').fetchone()
        finally:
            con.close()
        return bool(row) and row[0] == 'ok'
    except sqlite3.DatabaseError:
        return False


def bootstrap(data_dir):
    """First-run data: the `local` Tenant and an admin user.

    The admin password is generated, never fixed. The server binds 0.0.0.0 so
    projectors and scorers' phones on the venue LAN can reach it, which means a
    known default would hand full admin to anyone on that network — including a
    guest wifi it happens to share. The generated one is shown here and written to
    a file only readable on this laptop.

    Returns the password when one was created, else None.
    """
    from django.contrib.auth.models import User
    from mahj.models import Tenant

    subdomain = os.environ.get('LOCAL_TENANT', '').strip() or 'local'
    Tenant.objects.get_or_create(subdomain=subdomain, defaults={'name': subdomain})
    if User.objects.filter(is_superuser=True).exists():
        return None

    password = secrets.token_urlsafe(9)   # ~12 chars, typeable off a screen
    User.objects.create_superuser('admin', '', password)

    # Also on disk, because a console line scrolls away and the operator may only
    # come back to log in later. 0600, and the file says to delete it.
    note = data_dir / 'first-login.txt'
    try:
        note.write_text(
            "Mahjong Tournament Organizer — first login\n"
            "==========================================\n\n"
            f"  username: admin\n"
            f"  password: {password}\n\n"
            "Change the password in the app (Administration -> Users), then delete\n"
            "this file. Anyone who can read it can administer the tournament.\n",
            encoding='utf-8')
        os.chmod(note, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        note = None   # the printout below is then the only copy

    print("\n" + "=" * 62)
    print("  Created the admin account")
    print("    username: admin")
    print(f"    password: {password}")
    if note is not None:
        print(f"\n  Also saved to: {note}")
    print("  Change it in the app (Administration -> Users), then delete that file.")
    print("=" * 62)
    return password


def main():
    os.environ['DJANGO_SETTINGS_MODULE'] = SETTINGS_MODULE
    data_dir = app_data_dir()
    load_config(data_dir)

    path = db_path(data_dir)
    os.environ['MAHJ_DB_PATH'] = path

    if not integrity_ok(path):
        print("\n*** The database failed its integrity check. ***\n"
              f"Its file is: {path}\n"
              "Delete it and relaunch: a fresh database is created, and you can\n"
              "restore your tournament from a backup file under\n"
              "Administration -> Backup & restore.\n")
        sys.exit(1)

    import django
    django.setup()
    from django.core.management import call_command
    call_command('migrate', '--noinput', verbosity=0)
    bootstrap(data_dir)

    url = f'http://{OPEN_HOST}:{PORT}/options'
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"\nMahjong admin is running at {url}")
    print("Other devices on this network can open display screens via the laptop's "
          "LAN IP — the admin's Display page lists the exact URLs.")
    print("(Close this window to stop.)\n")

    import uvicorn
    uvicorn.run('apps.asgi:application', host=BIND_HOST, port=PORT, log_level='warning')


if __name__ == '__main__':
    main()
