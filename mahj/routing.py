from django.urls import re_path

from . import consumers

# The dot is allowed so grouped subdomains (e.g. a.test.mahj.ovh -> "a.test")
# route too; it's a valid channel-layer group-name character.
websocket_urlpatterns = [
    re_path(r'ws/display/(?P<subdomain>[a-zA-Z0-9_.-]{1,80})/$', consumers.TenantConsumer.as_asgi()),
    # Legacy alias kept so any existing clients on /ws/leaderboard/ continue to work.
    re_path(r'ws/leaderboard/(?P<subdomain>[a-zA-Z0-9_.-]{1,80})/$', consumers.TenantConsumer.as_asgi()),
    re_path(r'ws/scorers/(?P<subdomain>[a-zA-Z0-9_.-]{1,80})/$', consumers.ScorersConsumer.as_asgi()),
]
