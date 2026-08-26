"""Screenshot-run settings: the standalone profile (sqlite, in-process cache and
channel layer, WhiteNoise statics) pinned to the 'test' tenant so the fixtures
toolbar renders, with the scan pages enabled so they can be photographed.

LOCAL_TENANT and MAHJ_DB_PATH come from the environment (read by standalone.py);
see README.md in this directory for the full capture recipe."""
from apps.settings.standalone import *  # noqa: F401,F403

# The fill toolbars save one table at a time, milliseconds apart, and sqlite
# serialises writers: at the standalone profile's 10s busy timeout the odd save
# still lost the lock and 500'd, leaving a table blank. Nothing here is
# latency-sensitive, so wait much longer instead.
DATABASES['default']['OPTIONS'] = {'timeout': 45}

# Render the scorer QR and the /scan page for the screenshots. No scan worker
# runs here — the pages are only looked at, never used to submit a photo.
SCAN_ENABLED = True

# Surface request tracebacks on stderr while capturing.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'loggers': {'django.request': {'handlers': ['console'], 'level': 'ERROR'}},
}
