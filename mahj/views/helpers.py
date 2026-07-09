import os
import pathlib

from django.conf import settings
from django.core.cache import cache
from django.forms import ModelForm

from ..models import Seat, Tenant, TournamentSettings, Hand


BASE_DIR = pathlib.Path(__file__).resolve().parent.parent

VARIABLES_TTL = 300  # 5 minutes; invalidated on TournamentSettings writes via signals.
TENANT_TTL = 600     # 10 minutes; invalidated on Tenant writes via signals.


def get_counter(tenant):
    v = TournamentSettings.objects.filter(tenant=tenant).first()
    return v.counter if v else -1


def set_counter(tenant, value):
    # .update() skips signals: counter writes don't need to invalidate leaderboard cache.
    TournamentSettings.objects.filter(tenant=tenant).update(counter=value)
    if tenant is not None:
        cache.delete(f'variables:{tenant.subdomain}')


def is_scorer(user):
    return user.is_authenticated and (user.is_staff or user.groups.filter(name='Scorer').exists())

def is_display_op(user):
    return user.is_authenticated and (user.is_staff or user.groups.filter(name='Display_op').exists())

def is_publisher(user):
    return user.is_authenticated and (user.is_staff or user.groups.filter(name='Publisher').exists())

def is_scorer_or_display_op(user):
    return is_scorer(user) or is_display_op(user)

def can_access_admin(user):
    """Any role with a reason to open the admin dashboard: scorers, display
    operators and publishers (staff are included via each role check)."""
    return is_scorer_or_display_op(user) or is_publisher(user)


class PositionForm(ModelForm):
    class Meta:
        model = Seat
        fields = ['id', 'minipoints', 'tablepoints']


def lan_ip():
    """Best-effort primary LAN IPv4 of this machine, for the standalone Display
    page's "open screens on other devices" URLs. Uses the standard UDP-connect
    trick (no packets are actually sent); None if it can't be determined."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        return ip if ip and not ip.startswith('127.') else None
    except OSError:
        return None


def public_site_url(subdomain, public_url=''):
    """Public spectator-site URL to advertise (projector QR + caption, printed
    cards). The tenant's configured ``public_url`` (TournamentSettings) wins —
    set when the static site is published to an external host — otherwise the
    tenant's ``<subdomain>.<BASE_DOMAIN>``."""
    url = (public_url or '').strip().rstrip('/')
    if url:
        return url if '://' in url else f'https://{url}'
    return f'https://{subdomain}.{settings.BASE_DOMAIN}'


def public_site_host(subdomain, public_url=''):
    """`public_site_url` without the scheme, for a compact on-screen caption."""
    return public_site_url(subdomain, public_url).split('://', 1)[-1]


def get_domain(request):
    # Local instance: a venue laptop is reached at localhost / a bare LAN IP,
    # which carries no subdomain, so normal host parsing can't find the tenant.
    # LOCAL_TENANT pins every request to one tenant regardless of IP/subnet.
    #   - The standalone build sets settings.LOCAL_TENANT and is always honoured
    #     (that build is single-tenant by construction).
    #   - The DEBUG-gated env var is the dev/laptop-failover path; it stays gated
    #     so it can never collapse the multi-tenant cloud (prod) onto one tenant.
    forced = (getattr(settings, 'LOCAL_TENANT', '') or '').strip()
    if not forced and settings.DEBUG:
        forced = os.environ.get('LOCAL_TENANT', '').strip()
    if forced:
        return forced
    host = request.get_host()
    parts = host.split('.')
    if len(parts) >= 3:
        subdomain = parts[0]
    else:
        subdomain = ""
    return subdomain


def get_tenant(request):
    if hasattr(request, '_tenant'):
        return request._tenant
    subdomain = get_domain(request)
    cache_key = f'tenant:{subdomain}'
    tenant = cache.get(cache_key)
    if tenant is None:
        tenant = Tenant.objects.filter(subdomain=subdomain).first()
        # Auto-provision a tenant only in dev. In prod a typo'd subdomain must not
        # silently create one — create tenants explicitly via the Django admin.
        if tenant is None and settings.DEBUG and request.user.is_authenticated and request.user.is_staff:
            tenant = Tenant(subdomain=subdomain)
            tenant.save()
        if tenant is not None:
            cache.set(cache_key, tenant, TENANT_TTL)
    request._tenant = tenant
    return tenant


def get_variables(request):
    # Memoize on the request (like get_tenant): several context processors and
    # the view itself read the settings each request, so share one fetch — and
    # so a no-op cache backend (tests) can't turn that into repeated queries.
    if hasattr(request, '_variables'):
        return request._variables
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    cache_key = f'variables:{subdomain}'
    cached = cache.get(cache_key)
    if cached is None:
        cached = TournamentSettings.objects.filter(tenant=tenant).first()
        if cached is None:
            cached = TournamentSettings(tenant=tenant, welcome="Welcome")
            cached.save()
        cache.set(cache_key, cached, VARIABLES_TTL)
    request._variables = cached
    return cached


def player_statistics(request, player, variables):
    tenant = get_tenant(request)
    position_vals = Seat.objects.filter(tenant=tenant, draw_number=player.draw_number).order_by('round_nb')
    position_vals = [p for p in position_vals if p.minipoints is not None]
    hand_vals = []
    num_wins = {"num": 0, "round": ""}
    for position_val in position_vals:
        win_hands = Hand.objects.filter(tenant=tenant).order_by('id').filter(
            round_nb=position_val.round_nb,
            table_nb=position_val.table_nb,
            win_by=position_val.wind,
        )
        hand_vals += win_hands
        if len(win_hands) > num_wins["num"]:
            num_wins = {"num": len(win_hands), "round": position_val.round_nb}
    biggest_hands = sorted(
        [{"pts": h.points, "round": h.round_nb} for h in hand_vals],
        reverse=True, key=lambda x: x["pts"],
    )
    biggest_total = sorted(
        [{"pts": p.minipoints, "round": p.round_nb} for p in position_vals],
        reverse=True, key=lambda x: x["pts"],
    )
    return {
        "biggest_hands": biggest_hands,
        "biggest_total": biggest_total,
        "num_wins": num_wins,
    }


def get_podium(scores_json):
    podium = [[], [], []]
    for score in scores_json:
        if score["pos"] < 4:
            podium[score["pos"] - 1].append(score)
    return podium
