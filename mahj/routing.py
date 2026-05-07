from django.urls import re_path

from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/display/(?P<subdomain>[\w-]+)/$', consumers.TenantConsumer.as_asgi()),
    # Legacy alias kept so any existing clients on /ws/leaderboard/ continue to work.
    re_path(r'ws/leaderboard/(?P<subdomain>[\w-]+)/$', consumers.TenantConsumer.as_asgi()),
    re_path(r'ws/scorers/(?P<subdomain>[\w-]+)/$', consumers.ScorersConsumer.as_asgi()),
]
