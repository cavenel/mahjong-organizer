import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "apps.settings.prod")

# get_asgi_application() must be called before any import that triggers app loading.
_django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402
from mahj.routing import websocket_urlpatterns  # noqa: E402

# A WebSocket handshake is not covered by CSRF, so the Origin header is the only
# thing standing between a page on someone else's site and a socket opened with
# the visitor's cookies. Defence in depth: the session cookie is SameSite=Lax, so
# a cross-site handshake already arrives unauthenticated, and every consumer is
# read-only. Checked against ALLOWED_HOSTS, so it follows the deployment.
application = ProtocolTypeRouter({
    "http": _django_asgi_app,
    "websocket": AllowedHostsOriginValidator(
        AuthMiddlewareStack(URLRouter(websocket_urlpatterns))),
})
