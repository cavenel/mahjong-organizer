# PyInstaller spec for the standalone Mahjong admin binary.
#
# Build (per OS — there is no cross-platform binary):
#     pip install -r requirements/standalone.txt
#     python manage.py collectstatic --noinput --settings apps.settings.standalone
#     pyinstaller standalone/mahj.spec
#
# Produces dist/mahj-organizer/ (a one-folder app; the launcher is mahj-organizer[.exe]).
# Django loads templates, static files, migrations and app configs dynamically,
# and apps.asgi is referenced by string, so none of that is traced from run.py —
# it's bundled as data and forced in via hiddenimports below. The scan/OCR stack
# and the Redis/Postgres backends are excluded to keep the binary small; the
# standalone settings never load them (see apps/settings/standalone.py).
import os
import pkgutil
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

project_root = os.path.abspath(os.getcwd())

# PyInstaller execs this spec without the project root on sys.path, so the
# `import apps` / `import mahj` below (and django.setup()) would fail. Add it.
# (pathex only affects PyInstaller's own analysis, not this spec's imports.)
if project_root not in sys.path:
    sys.path.insert(0, project_root)


def walk_package(name):
    """Every submodule of `name`, found by walking its filesystem tree.

    collect_submodules() is unreliable for our app: importing mahj.views (which
    pulls in the ORM) trips AppRegistryNotReady inside its protected import, and
    it silently returns zero view modules. A plain pkgutil walk after
    django.setup() (below) enumerates them correctly.
    """
    pkg = __import__(name, fromlist=['__path__'])
    return [mi.name for mi in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + '.')]

# Configure Django before we enumerate submodules: collect_submodules imports
# each module, and importing mahj.models / views needs the app registry ready.
# Dummy values satisfy the settings that read the environment at import time.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'apps.settings.standalone')
os.environ.setdefault('DJANGO_SECRET_KEY', 'build-time-only')
os.environ.setdefault('ALLOWED_HOSTS', 'localhost')
import django  # noqa: E402
django.setup()

# --- data files -------------------------------------------------------------
datas = []
datas += collect_data_files('mahj', include_py_files=False)   # templates, images, js/css
datas += collect_data_files('django', include_py_files=False)  # admin templates, etc.
datas += collect_data_files('channels', include_py_files=False)
datas += [(os.path.join(project_root, 'staticfiles'), 'staticfiles')]  # collectstatic output

# --- hidden imports ----------------------------------------------------------
# mahj: views, migrations, publish, management commands — minus scan_worker,
# which pulls in the redis/OCR deps we deliberately drop (never run here), and
# minus the test suite, which would drag pytest into the binary uninstalled.
mahj_mods = [
    m for m in walk_package('mahj')
    if 'scan_worker' not in m and not m.startswith('mahj.tests')
]
# uvicorn ships optional server backends we don't use and don't bundle:
# .workers (gunicorn worker class) and the wsproto websocket impl (we use the
# 'websockets' impl). Drop them so the build doesn't chase excluded deps.
uvicorn_mods = [
    m for m in collect_submodules('uvicorn')
    if 'workers' not in m and 'wsproto' not in m
]
hiddenimports = mahj_mods + collect_submodules('channels') + uvicorn_mods
hiddenimports += collect_submodules('sesame')
# whitenoise middleware + storage are referenced only by string in settings
# (MIDDLEWARE / STORAGES), so grab the whole package rather than name each one.
hiddenimports += collect_submodules('whitenoise')
# django-mathfilters: an INSTALLED_APP whose templatetags load by string
# ({% load mathfilters %}), so its templatetags submodule must be bundled.
hiddenimports += collect_submodules('mathfilters')
hiddenimports += [
    # apps.* is reached only via the "apps.asgi:application" string, so list the
    # pieces explicitly. (Not collect_submodules('apps') — that would import
    # apps.settings.prod, which raises without real prod env vars.)
    'apps.asgi', 'apps.wsgi', 'apps.urls', 'apps.middleware', 'apps.csrf',
    'apps.settings.standalone', 'apps.settings.base',
    'django.db.backends.sqlite3',
    'django.contrib.staticfiles',
    'daphne',
    'dotenv',
    'paramiko',
    'segno',
]

# --- excludes (never loaded by the standalone profile) -----------------------
excludes = [
    'cv2', 'numpy', 'PIL', 'anthropic',        # scan / OCR (SCAN_ENABLED=False)
    'psycopg2', 'psycopg2-binary',             # Postgres
    'channels_redis', 'django_redis', 'redis',  # Redis cache + bus
    'weasyprint',                              # docs PDFs (build-time only)
    'gunicorn',
]

block_cipher = None

a = Analysis(
    [os.path.join(project_root, 'standalone', 'run.py')],
    pathex=[project_root],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='mahj-organizer',
    console=True,          # the console window is how the operator stops the app
    disable_windowed_traceback=False,
)

coll = COLLECT(exe, a.binaries, a.datas, name='mahj-organizer')
