import json
import pathlib
from functools import wraps

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.core.cache import cache
from django.core.exceptions import BadRequest
from django.http import HttpResponseForbidden, JsonResponse

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


class FieldError(Exception):
    """One request field that didn't parse, named.

    Deliberately not ``BadRequest``: that is for a request that is malformed as a
    request (see :func:`json_body`), and Django logs those with a full traceback and
    renders a generic 400 that drops the message. A scorer mistyping a cell is an
    expected outcome of a human typing, not an exceptional condition — it deserves a
    400 that says *which* cell, and no stack trace in the log.

    ``apps.middleware.FieldErrorMiddleware`` turns this into that JSON 400, so no
    view needs a try/except around its field reads.
    """

    def __init__(self, field, message):
        super().__init__(f"'{field}': {message}")
        self.field = field
        self.message = message


def method_not_allowed():
    """The one answer a mutating endpoint gives a non-POST request.

    These are all XHR endpoints whose front-ends read ``responseJSON.error``, so a
    plain-text body (or a 400/403 standing in for a wrong method) left the page
    with an unexplained failure. For a page a browser navigates to (not XHR), use
    Django's ``HttpResponseNotAllowed`` instead — JSON is the wrong body there.
    """
    return JsonResponse(
        {'status': 'method_not_allowed', 'error': 'POST required'}, status=405)


def int_param(data, field, default=_REQUIRED):
    """One integer field out of ``request.POST`` or a :func:`json_body` dict.

    Absent or blank yields ``default``; with no default that is a ``FieldError``.
    Present but not an integer is always a ``FieldError``. Either way the client
    gets a 400 naming the field, rather than a 500 out of a bare ``int()``.
    """
    raw = data.get(field)
    if raw is None or raw == '':
        if default is _REQUIRED:
            raise FieldError(field, 'is required')
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise FieldError(field, f'must be a whole number, got {raw!r}')


def number_or_none(data, field, cast=int):
    """One numeric score cell that may legitimately be empty.

    Absent or blank yields ``None`` — the cell is cleared, stored NULL. Anything
    else must parse as ``cast`` or it is a ``FieldError`` naming the field: a cell
    the scorer can see a value in must never be quietly stored as NULL, which
    reads downstream as "not played yet".
    """
    raw = data.get(field)
    if raw is None or raw == '':
        return None
    try:
        return cast(raw)
    except (TypeError, ValueError):
        raise FieldError(field, f'must be a number, got {raw!r}')


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


# Session key for "View as": a tenant admin previewing the console as one of the
# single-role accounts they hand out. Holds one of _TENANT_ROLES, or is absent.
VIEW_AS_SESSION_KEY = 'view_as_role'


class _ViewAsMembership:
    """Synthetic single-role membership an admin wears while previewing the
    console as a scorer / publisher / display operator. Never admin: the point is
    to see exactly what that account sees — nav, pages, 403s and all."""
    is_tenant_admin = False

    def __init__(self, role):
        self.role = role
        self.is_scorer = role == 'scorer'
        self.is_display_op = role == 'display_op'
        self.is_publisher = role == 'publisher'


def current_membership(request):
    """The request user's Membership for the current subdomain's tenant, or None.

    Memoized on ``request._membership`` (like get_tenant/get_tournament) so the
    decorators, view bodies and context processor share one lookup. Anonymous
    users and users with no row for this tenant get None with no query for the
    anonymous case; superusers get a synthetic all-true membership (they bypass).

    "View as": when the session asks for a single role *and the real membership
    is admin-tier*, the admin's membership is swapped for a synthetic one holding
    only that role, so every check downstream sees the preview. The real row is
    kept on ``request._real_membership`` for the few places that must know who
    is actually here (the View-as menu itself). The swap keys on the real
    membership, so a session key planted by a non-admin changes nothing."""
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
    request._real_membership = membership
    session = getattr(request, 'session', None)
    view_as = session.get(VIEW_AS_SESSION_KEY) if session is not None else None
    if view_as in _TENANT_ROLES and membership is not None and membership.is_tenant_admin:
        membership = _ViewAsMembership(view_as)
    request._membership = membership
    return membership


def viewing_as(request):
    """The role an admin is currently previewing as, or None."""
    m = current_membership(request)
    return m.role if isinstance(m, _ViewAsMembership) else None


def real_is_tenant_admin(request):
    """Admin over the current tenant *ignoring* any View-as preview — who is
    actually holding the session. Everything else should use is_tenant_admin."""
    current_membership(request)
    m = request._real_membership
    return bool(m and m.is_tenant_admin)


def acting_superuser(request):
    """Platform superuser, unless previewing as a role — a superuser viewing as a
    scorer should not keep the superuser-only entries and pages."""
    user = getattr(request, 'user', None)
    return bool(user is not None and user.is_authenticated and user.is_superuser
                and viewing_as(request) is None)


def is_tenant_admin(request):
    """Full admin over the current tenant (or a platform superuser)."""
    m = current_membership(request)
    return bool(m and m.is_tenant_admin)


def has_role(request, *roles):
    """True if the user holds any of ``roles`` on the current tenant. Tenant
    admins (and superusers) implicitly hold every app role, mirroring the old
    "staff implies scorer/display/publisher".

    Publisher also implies scorer: a publisher already sees every unpublished
    score and can lock or reopen the scorers' work, so withholding score *edits*
    protected nothing and only forced them to fetch a scorer for a typo they'd
    spotted while reconciling. The implication lives here, not in the row, so a
    membership stays an honest record of what was granted."""
    m = current_membership(request)
    if not m:
        return False
    if m.is_tenant_admin:
        return True
    if 'scorer' in roles and m.is_publisher:
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
    """Platform-operator gate (tenant CRUD)."""
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
    # The standalone build is reached at localhost, which carries no subdomain, so
    # normal host parsing can't find a tenant. That build is single-tenant by
    # construction and sets settings.LOCAL_TENANT, which pins every request to it.
    forced = (getattr(settings, 'LOCAL_TENANT', '') or '').strip()
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
        # is_superuser, not is_staff: access-control.md reserves the staff flag for
        # the Django admin site and forbids keying a decision on it.
        if tenant is None and settings.DEBUG and request.user.is_authenticated and request.user.is_superuser:
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
        if tenant is None:
            # An unknown subdomain resolves to tenant=None; saving that hits the NOT
            # NULL constraint and 500s every page on the subdomain (reachable via the
            # *.BASE_DOMAIN wildcard). Serve a transient default instead.
            cached = TournamentSettings(tenant=None, welcome="Welcome")
        else:
            # get_or_create, so two workers arriving on a fresh tenant together
            # can't each provision a row — the loser of the race gets the winner's.
            cached, _ = TournamentSettings.objects.get_or_create(
                tenant=tenant, defaults={'welcome': "Welcome"})
        cache.set(cache_key, cached, TOURNAMENT_TTL)
    request._tournament = cached
    return cached
