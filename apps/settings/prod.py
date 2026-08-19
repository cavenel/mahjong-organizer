import os
from .base import *

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]   # no default — fails loudly if unset
DEBUG = False
# Default to the apex domain and all its subdomains; ALLOWED_HOSTS env overrides.
ALLOWED_HOSTS = (
    os.environ["ALLOWED_HOSTS"].split(",")
    if os.environ.get("ALLOWED_HOSTS")
    else [BASE_DOMAIN, f".{BASE_DOMAIN}"]
)

SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# Trust the X-Forwarded-Proto header set by nginx so Django knows the original
# scheme is HTTPS. Without this, SECURE_SSL_REDIRECT causes a redirect loop.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# USE_X_FORWARDED_HOST stays off. nginx already passes the real Host on every
# proxy block (`proxy_set_header Host $host`) and never sets X-Forwarded-Host, so
# turning it on would make Django prefer a header only the *client* can supply —
# and the subdomain in that host is what picks the tenant. Leave it unset unless
# nginx starts setting X-Forwarded-Host itself, on every block.

# nginx serves /static/ directly in prod, so WhiteNoise would never serve a
# request here. Drop the middleware to avoid the dead per-request hop.
# (collectstatic still uses CompressedStaticFilesStorage from base.py.)
MIDDLEWARE = [m for m in MIDDLEWARE if 'whitenoise' not in m.lower()]
