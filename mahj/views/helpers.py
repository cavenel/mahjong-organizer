import json
import os
import pathlib
from functools import wraps

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.core.cache import cache
from django.core.exceptions import BadRequest
from django.http import HttpResponseForbidden

from ..models import Membership, Tenant, TournamentSettings


BASE_DIR = pathlib.Path(__file__).resolve().parent.parent

TOURNAMENT_TTL = 300  # 5 minutes; invalidated on TournamentSettings writes via signals.
TENANT_TTL = 600     # 10 minutes; invalidated on Tenant writes via signals.


def json_body(request):
    """The request body parsed as a JSON object (dict). An empty body is ``{}``.

    Malformed JSON or a non-object payload raises ``BadRequest``, which Django
    turns into a plain 400 — every JSON endpoint's real client sends an object,
    so only crafted or buggy requests hit that path and none owes it a prettier
    answer. Endpoint-specific validation (missing/invalid fields) stays in the
    view, on the dict this returns."""
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except ValueError:
        raise BadRequest('Malformed JSON body')
    if not isinstance(data, dict):
        raise BadRequest('JSON body must be an object')
    return data


_REQUIRED = object()


def int_param(data, field, default=_REQUIRED):
    """One integer field out of ``request.POST`` or a :func:`json_body` dict.

    Absent or blank yields ``default``; with no default that is a ``BadRequest``.
    Present but not an integer is always a ``BadRequest``. Both name the field, so
    a crafted or buggy request gets a 400 saying what was wrong instead of a 500
    out of a bare ``int()``. As with :func:`json_body` the message reaches the
    server log rather than the response body — Django renders a plain 400.
    """
    raw = data.get(field)
    if raw is None or raw == '':
        if default is _REQUIRED:
            raise BadRequest(f"'{field}' is required")
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise BadRequest(f"'{field}' must be a whole number, got {raw!r}")


def number_or_none(data, field, cast=int):
    """One numeric score cell that may legitimately be empty.

    Absent or blank yields ``None`` — the cell is cleared, stored NULL. Anything
    else must parse as ``cast`` or it is a ``BadRequest`` naming the field: a cell
    the scorer can see a value in must never be quietly stored as NULL, which
    reads downstream as "not played yet".
    """
    raw = data.get(field)
    if raw is None or raw == '':
        return None
    try:
        return cast(raw)
    except (TypeError, ValueError):
        raise BadRequest(f"'{field}' must be a number, got {raw!r}")


def get_counter(tenant):
    v = TournamentSettings.objects.filter(tenant=tenant).first()
    return v.counter if v else -1


def set_counter(tenant, value):
    # .update() skips signals: counter writes don't need to invalidate leaderboard cache.
    TournamentSettings.objects.filter(tenant=tenant).update(counter=value)
    if tenant is not None:
        cache.delete(f'tournament:{tenant.subdomain}')


# ---------------------------------------------------------------------------
# Per-tenant authorization.
#
# Roles are scoped to a tenant via Membership (see docs/dev/access-control.md).
# Every check is evaluated against the CURRENT subdomain's tenant, so a user's
# access on one tenant says nothing about another — cross-tenant isolation is
# just the membership row's absence. Platform superusers bypass membership.
# ---------------------------------------------------------------------------

# The four role flags on Membership, and the tenant-role subset the *_required
# decorators / has_role accept (tenant_admin is checked on its own).
_TENANT_ROLES = ('scorer', 'display_op', 'publisher')


class _SuperuserMembership:
    """Synthetic all-true membership for platform superusers, who are cross-tenant
    and need no row. Lets every check read the same ``.is_*`` attributes whether
    the caller is a superuser or a real member."""
    is_tenant_admin = is_scorer = is_display_op = is_publisher = True


def current_membership(request):
    """The request user's Membership for the current subdomain's tenant, or None.

    Memoized on ``request._membership`` (like get_tenant/get_tournament) so the
    decorators, view bodies and context processor share one lookup. Anonymous
    users and users with no row for this tenant get None with no query for the
    anonymous case; superusers get a synthetic all-true membership (they bypass)."""
    if hasattr(request, '_membership'):
        return request._membership
    user = getattr(request, 'user', None)
    membership = None
    if user is not None and user.is_authenticated:
        if user.is_superuser:
            membership = _SuperuserMembership()
        else:
            tenant = get_tenant(request)
            if tenant is not None:
                membership = Membership.objects.filter(user=user, tenant=tenant).first()
    request._membership = membership
    return membership


def is_tenant_admin(request):
    """Full admin over the current tenant (or a platform superuser)."""
    m = current_membership(request)
    return bool(m and m.is_tenant_admin)


def has_role(request, *roles):
    """True if the user holds any of ``roles`` on the current tenant. Tenant
    admins (and superusers) implicitly hold every app role, mirroring the old
    "staff implies scorer/display/publisher"."""
    m = current_membership(request)
    if not m:
        return False
    if m.is_tenant_admin:
        return True
    return any(getattr(m, f'is_{r}', False) for r in roles)


def can_access_admin(request):
    """Any role with a reason to open the admin dashboard: tenant admins, scorers,
    display operators and publishers (superusers included via the membership)."""
    return has_role(request, *_TENANT_ROLES)


def _deny(request):
    """Rejection shared by the *_required decorators. Anonymous users get the usual
    login redirect (preserving the ``/accounts/login/`` flow); an authenticated
    user who simply lacks the role — including a member of a *different* tenant on
    this subdomain — gets a 403, which is exactly the isolation we want."""
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    return HttpResponseForbidden()


def superuser_required(view):
    """Platform-operator gate (tenant CRUD, whole-cluster restore)."""
    @wraps(view)
    def inner(request, *args, **kwargs):
        if request.user.is_authenticated and request.user.is_superuser:
            return view(request, *args, **kwargs)
        return _deny(request)
    return inner


def tenant_admin_required(view):
    """Full admin over the request's tenant (superuser or ``is_tenant_admin``)."""
    @wraps(view)
    def inner(request, *args, **kwargs):
        if is_tenant_admin(request):
            return view(request, *args, **kwargs)
        return _deny(request)
    return inner


def tenant_role_required(*roles):
    """Any listed role on the request's tenant (superuser or admin also pass)."""
    def decorator(view):
        @wraps(view)
        def inner(request, *args, **kwargs):
            if has_role(request, *roles):
                return view(request, *args, **kwargs)
            return _deny(request)
        return inner
    return decorator


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
    host = request.get_host().split(':')[0]   # drop any :port
    base = settings.BASE_DOMAIN
    # The tenant is everything to the left of the base domain, so a single
    # DNS/cert wildcard (*.mahj.ovh) and a grouped one (*.test.mahj.ovh) both
    # resolve: a.mahj.ovh -> "a", a.test.mahj.ovh -> "a.test". The apex itself
    # and any host outside the base domain (bare IP, LAN name) carry no tenant.
    if host == base or not host.endswith('.' + base):
        return ""
    return host[: -(len(base) + 1)]


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


def get_tournament(request):
    # Memoize on the request (like get_tenant): several context processors and
    # the view itself read the settings each request, so share one fetch — and
    # so a no-op cache backend (tests) can't turn that into repeated queries.
    if hasattr(request, '_tournament'):
        return request._tournament
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    cache_key = f'tournament:{subdomain}'
    cached = cache.get(cache_key)
    if cached is None:
        cached = TournamentSettings.objects.filter(tenant=tenant).first()
        if cached is None:
            cached = TournamentSettings(tenant=tenant, welcome="Welcome")
            # Persist (and lazily provision) only for a real tenant. An unknown
            # subdomain resolves to tenant=None; saving that hits the NOT NULL
            # constraint and 500s every page on the subdomain (now reachable via
            # the *.BASE_DOMAIN wildcard). Serve a transient default instead.
            if tenant is not None:
                cached.save()
        cache.set(cache_key, cached, TOURNAMENT_TTL)
    request._tournament = cached
    return cached
