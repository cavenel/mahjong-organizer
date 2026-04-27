import os
from .base import *

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]   # no default — fails loudly if unset
DEBUG = False
ALLOWED_HOSTS = os.environ["ALLOWED_HOSTS"].split(",")

SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# Trust the X-Forwarded-Proto header set by nginx so Django knows the original
# scheme is HTTPS. Without this, SECURE_SSL_REDIRECT causes a redirect loop.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True
