import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "apps.settings.prod")

# get_asgi_application() must be called before any import that triggers app loading.
_django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from mahj.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter({
    "http": _django_asgi_app,
    "websocket": AuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
})
