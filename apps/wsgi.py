"""
WSGI config for apps project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/2.0/howto/deployment/wsgi/
"""

import os, sys

from django.core.wsgi import get_wsgi_application

sys.path.append('/home/cavenel/django/apps') 

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "apps.settings.prod")

#from weberror.errormiddleware import make_error_middleware

application = get_wsgi_application()
#make_error_middleware(application)