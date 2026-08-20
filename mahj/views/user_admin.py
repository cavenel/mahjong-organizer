"""Per-tenant user management: create accounts, grant tenant-scoped roles, and
mint/revoke passwordless login links (django-sesame).

Two modes, gated differently:
  * Tenant-admin mode — a tenant admin (``Membership.is_tenant_admin``) manages
    the users of *their own* tenant. Everything here is scoped to
    ``get_tenant(request)``; users of other tenants are invisible. A tenant admin
    may freely add/remove a user's membership in their tenant, but may only rotate
    credentials (revoke links) or delete the account for users whose memberships
    are entirely within this tenant (the *containment rule*) — a shared account is
    credential-managed only by a superuser. This bounds the blast radius.
  * Superuser mode — platform operator: create/rename tenants and seed a tenant's
    first admin (needs a tenant selector, since a superuser isn't tied to a
    subdomain).

Links are stateless sesame tokens, so two constraints shape this module:
  * validity is the single global ``SESAME_MAX_AGE`` setting (no per-link TTL);
  * "revoking" a user's links rotates their password hash, which invalidates
    every link that user holds at once (per-user, not per-link). Links are global
    auth but gated by membership, so a tenant-A link on tenant-B's subdomain
    yields no access — no per-tenant handling is needed at mint time.
"""

import time
from functools import wraps

from django.conf import settings
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from sesame.utils import get_token

from ..models import Membership, Tenant, TournamentSettings
from .helpers import get_tenant, is_tenant_admin, json_body, superuser_required, tenant_admin_required

# Tier-3 role flags the console toggles (tenant_admin is handled as its own flag).
TENANT_ROLES = ['scorer', 'display_op', 'publisher']

# "Sudo mode": an admin must re-enter their password to reach user management, so
# an unattended/borrowed admin session can't be used to create or steal accounts.
# The confirmation is stamped in the session and only lasts this long.
REAUTH_SESSION_KEY = 'users_reauth_at'
USERS_REAUTH_MAX_AGE = 600  # seconds


def reauth_ok(request):
    """True if this session has confirmed the operator's password within the last
    ``USERS_REAUTH_MAX_AGE`` seconds. Link-only accounts (no usable password) have
    nothing to confirm, so they can never satisfy this and are kept out of user
    management entirely."""
    user = request.user
    if not user.has_usable_password():
        return False
    ts = request.session.get(REAUTH_SESSION_KEY)
    return bool(ts) and (time.time() - ts) < USERS_REAUTH_MAX_AGE


def tenant_admin_and_reauthed(view):
    """Like ``tenant_admin_required``, but also requires a recent password
    re-confirmation. Mutating endpoints carry this so they can't be driven directly
    (bypassing the page-level gate) from a stale session. Anonymous → login
    redirect and an authenticated non-admin → 403 (both from
    ``tenant_admin_required``); an admin whose confirmation has lapsed gets a JSON
    ``reauth_required`` the front-end can act on."""
    @wraps(view)
    def inner(request, *args, **kwargs):
        if not reauth_ok(request):
            return JsonResponse({'status': 'reauth_required'}, status=403)
        return view(request, *args, **kwargs)
    return tenant_admin_required(inner)


def superuser_and_reauthed(view):
    """Like ``tenant_admin_and_reauthed``, but requires ``is_superuser``.

    Platform-operator actions (tenant CRUD, seeding admins, the standalone
    build's whole-database snapshot restore) live behind this: they reach past one
    tenant, so they must never be reachable by a per-tenant admin — the superuser
    flag is the only cross-tenant guard."""
    @wraps(view)
    def inner(request, *args, **kwargs):
        if not reauth_ok(request):
            return JsonResponse({'status': 'reauth_required'}, status=403)
        return view(request, *args, **kwargs)
    return superuser_required(inner)


@tenant_admin_required
def user_reauth(request):
    """Confirm the current user's password and stamp the session."""
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    data = json_body(request)
    if not request.user.check_password(data.get('password') or ''):
        return JsonResponse({'status': 'error', 'error': 'incorrect password'}, status=403)
    request.session[REAUTH_SESSION_KEY] = time.time()
    return JsonResponse({'status': 'ok'})


def _set_membership(user, tenant, roles, is_admin):
    """Make ``user``'s Membership in ``tenant`` carry exactly these roles
    (unknown role names are ignored). Creates the row if absent."""
    wanted = [r for r in roles if r in TENANT_ROLES]
    flags = {'is_tenant_admin': bool(is_admin)}
    for r in TENANT_ROLES:
        flags[f'is_{r}'] = r in wanted
    Membership.objects.update_or_create(user=user, tenant=tenant, defaults=flags)


def _memberships_contained(user, tenant):
    """True if every membership this user holds is within ``tenant`` — i.e. the
    account isn't shared with another tenant. Credential-management (revoke links,
    delete account) is allowed only for contained accounts."""
    return not Membership.objects.filter(user=user).exclude(tenant=tenant).exists()


def _tenant_admin_count(tenant):
    return Membership.objects.filter(tenant=tenant, is_tenant_admin=True).count()


def _target_in_tenant(request, data):
    """Resolve the target user for a tenant-scoped action and confirm they have a
    membership in the request's tenant. Returns ``(user, tenant, membership)`` or
    an error ``JsonResponse``. Keeps other tenants' users invisible: a user with no
    membership here is reported as 'not found' rather than 'forbidden'."""
    tenant = get_tenant(request)
    if tenant is None:
        return JsonResponse({'status': 'error', 'error': 'no tenant'}, status=400)
    try:
        user = User.objects.get(pk=data.get('user_id'))
    except (User.DoesNotExist, ValueError, TypeError):
        return JsonResponse({'status': 'error', 'error': 'user not found'}, status=404)
    membership = Membership.objects.filter(user=user, tenant=tenant).first()
    if membership is None:
        return JsonResponse({'status': 'error', 'error': 'user not found'}, status=404)
    return user, tenant, membership


# --------------------------------------------------------------------------
# Tenant-admin mode — scoped to get_tenant(request)
# --------------------------------------------------------------------------

@tenant_admin_and_reauthed
def user_create(request):
    """Create an account and its Membership in the current tenant."""
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    data = json_body(request)
    tenant = get_tenant(request)
    if tenant is None:
        return JsonResponse({'status': 'error', 'error': 'no tenant'}, status=400)

    username = (data.get('username') or '').strip()
    if not username:
        return JsonResponse({'status': 'error', 'error': 'username required'}, status=400)
    # Usernames are globally unique (Django constraint). Reject collisions with a
    # generic message — don't leak whether/which other tenant already has one.
    if User.objects.filter(username=username).exists():
        return JsonResponse({'status': 'error', 'error': 'username already taken'}, status=400)

    with transaction.atomic():
        user = User(username=username)
        password = data.get('password') or ''
        if password:
            user.set_password(password)
        else:
            # Passwordless: the account can only log in via a sesame link.
            user.set_unusable_password()
        try:
            user.save()
        except IntegrityError:
            return JsonResponse({'status': 'error', 'error': 'username already taken'}, status=400)
        _set_membership(user, tenant, data.get('roles', []), data.get('is_tenant_admin'))

    return JsonResponse({'status': 'ok', 'id': user.id, 'username': user.username})


@superuser_and_reauthed
def user_add_existing(request):
    """Give an account that already exists a Membership in the current tenant.

    Superuser-only, deliberately — this is the one way to create a *shared* account,
    and handing it to tenant admins would undo an isolation property the rest of the
    module works to keep. Elsewhere a user outside this tenant is reported as "not
    found" rather than "forbidden" (see ``_target_in_tenant``) precisely so a tenant
    admin can't learn that an account exists somewhere else; a by-username add would
    turn that into an enumeration oracle they could probe. A superuser already spans
    tenants, and credential containment already makes shared accounts theirs to
    manage, so this belongs to them.

    Idempotent: an account already here just has its roles set, so this doubles as
    "re-grant" without a separate endpoint.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    data = json_body(request)
    tenant = get_tenant(request)
    if tenant is None:
        return JsonResponse({'status': 'error', 'error': 'no tenant'}, status=400)

    username = (data.get('username') or '').strip()
    if not username:
        return JsonResponse({'status': 'error', 'error': 'username required'}, status=400)
    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        return JsonResponse(
            {'status': 'error', 'error': f'No account named {username!r}.'}, status=404)

    already_here = Membership.objects.filter(user=user, tenant=tenant).exists()
    _set_membership(user, tenant, data.get('roles', []), data.get('is_tenant_admin'))
    return JsonResponse({
        'status': 'ok', 'id': user.id, 'username': user.username,
        # So the page can say "added" vs "roles updated" rather than guessing.
        'already_here': already_here,
        # True once this account belongs somewhere else too: from here on its
        # credentials are superuser-managed.
        'shared': Membership.objects.filter(user=user).exclude(tenant=tenant).exists(),
    })


@tenant_admin_and_reauthed
def user_update_roles(request):
    """Set the target's roles within the current tenant."""
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    data = json_body(request)

    resolved = _target_in_tenant(request, data)
    if isinstance(resolved, JsonResponse):
        return resolved
    user, tenant, membership = resolved

    is_admin = bool(data.get('is_tenant_admin'))
    # Guard: don't let an admin strip their own admin flag and lock themselves out.
    if user.id == request.user.id and not is_admin and not request.user.is_superuser:
        return JsonResponse({'status': 'error', 'error': 'you cannot remove your own admin access'}, status=400)
    # Guard: don't let a tenant strand itself by demoting its last admin. A
    # superuser is exempt — they're the recovery path and can re-seed an admin.
    if (membership.is_tenant_admin and not is_admin
            and not request.user.is_superuser and _tenant_admin_count(tenant) <= 1):
        return JsonResponse({'status': 'error', 'error': 'cannot remove the last admin of this tenant'}, status=400)

    _set_membership(user, tenant, data.get('roles', []), is_admin)
    return JsonResponse({'status': 'ok'})


@tenant_admin_and_reauthed
def user_generate_link(request):
    """Mint a passwordless login link for a user in this tenant.

    Containment-restricted, like ``user_revoke_links`` and ``user_delete``: the
    minted link is a full credential for the *account* and it is handed to the
    admin who asked for it, not to the user. So for an account shared with another
    tenant, minting would let an admin here open that link on the other tenant's
    subdomain and act with whatever roles the account holds there. Membership
    gating doesn't help — it is the account's own membership that grants the
    access. Only a superuser, who already spans tenants, may mint for a shared
    account.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    data = json_body(request)

    resolved = _target_in_tenant(request, data)
    if isinstance(resolved, JsonResponse):
        return resolved
    user, tenant, _membership = resolved

    if not request.user.is_superuser and not _memberships_contained(user, tenant):
        return JsonResponse(
            {'status': 'error',
             'error': 'shared account — only a superuser can mint its login links'},
            status=403)

    # Land the kiosk straight on the scoring page; the sesame middleware logs the
    # user in from the token on the way through. token chars are URL-safe.
    url = request.build_absolute_uri('/admin?page=scoring&sesame=' + get_token(user))
    return JsonResponse({'status': 'ok', 'url': url})


@tenant_admin_and_reauthed
def user_revoke_links(request):
    """Rotate the target's password hash, invalidating every sesame token they
    hold. Containment-restricted: rotating a *shared* account's credentials would
    reach beyond this tenant, so it's refused for accounts that also belong to
    another tenant (a superuser can still do it)."""
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    data = json_body(request)

    resolved = _target_in_tenant(request, data)
    if isinstance(resolved, JsonResponse):
        return resolved
    user, tenant, _membership = resolved

    if not request.user.is_superuser and not _memberships_contained(user, tenant):
        return JsonResponse(
            {'status': 'error', 'error': 'shared account — only a superuser can revoke its links'},
            status=403)

    # Rotating the password hash invalidates every existing sesame token for this
    # user. It also clears any usable password, so the account becomes link-only.
    user.set_unusable_password()
    user.save(update_fields=['password'])
    return JsonResponse({'status': 'ok'})


@tenant_admin_and_reauthed
def user_delete(request):
    """Delete a user account. Containment-restricted: a shared account can't be
    deleted from one tenant (use *remove from tenant* instead)."""
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    data = json_body(request)

    resolved = _target_in_tenant(request, data)
    if isinstance(resolved, JsonResponse):
        return resolved
    user, tenant, membership = resolved

    if user.id == request.user.id:
        return JsonResponse({'status': 'error', 'error': 'you cannot delete your own account'}, status=400)
    if not request.user.is_superuser and not _memberships_contained(user, tenant):
        return JsonResponse(
            {'status': 'error', 'error': 'shared account — remove from this tenant instead'},
            status=403)
    # Last-admin guard: a tenant can't delete its own last admin; a superuser can
    # (they can always re-seed one).
    if (membership.is_tenant_admin and not request.user.is_superuser
            and _tenant_admin_count(tenant) <= 1):
        return JsonResponse({'status': 'error', 'error': 'cannot delete the last admin of this tenant'}, status=400)

    user.delete()
    return JsonResponse({'status': 'ok'})


@tenant_admin_and_reauthed
def user_remove_from_tenant(request):
    """Remove the target's membership in this tenant, keeping the account (the
    containment-safe alternative to delete for shared accounts)."""
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    data = json_body(request)

    resolved = _target_in_tenant(request, data)
    if isinstance(resolved, JsonResponse):
        return resolved
    user, tenant, membership = resolved

    if user.id == request.user.id:
        return JsonResponse({'status': 'error', 'error': 'you cannot remove yourself'}, status=400)
    if (membership.is_tenant_admin and not request.user.is_superuser
            and _tenant_admin_count(tenant) <= 1):
        return JsonResponse({'status': 'error', 'error': 'cannot remove the last admin of this tenant'}, status=400)

    membership.delete()
    return JsonResponse({'status': 'ok'})


# --------------------------------------------------------------------------
# Superuser mode — tenant CRUD (multi-tenant cloud build only)
# --------------------------------------------------------------------------

def _clean_subdomain(value):
    """A subdomain is a DNS label used as the tenant key: lowercase, no dots or
    spaces. Returns the cleaned value or ''."""
    return (value or '').strip().lower()


def _standalone_blocked():
    """Tenant CRUD is meaningless in the single-tenant standalone build (the tenant
    is pinned via LOCAL_TENANT), so the endpoints 404 there — matching how the page
    is hidden from the nav."""
    if settings.STANDALONE:
        return JsonResponse({'status': 'error', 'error': 'Not available in this build.'}, status=404)
    return None


@superuser_and_reauthed
def tenant_create(request):
    blocked = _standalone_blocked()
    if blocked is not None:
        return blocked
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    data = json_body(request)
    name = (data.get('name') or '').strip()
    subdomain = _clean_subdomain(data.get('subdomain'))
    if not name or not subdomain:
        return JsonResponse({'status': 'error', 'error': 'name and subdomain required'}, status=400)
    if Tenant.objects.filter(subdomain=subdomain).exists():
        return JsonResponse({'status': 'error', 'error': 'subdomain already taken'}, status=400)
    tenant = Tenant.objects.create(name=name, subdomain=subdomain)
    return JsonResponse({'status': 'ok', 'id': tenant.id, 'subdomain': tenant.subdomain})


@superuser_and_reauthed
def tenant_rename(request):
    blocked = _standalone_blocked()
    if blocked is not None:
        return blocked
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    data = json_body(request)
    try:
        tenant = Tenant.objects.get(pk=data.get('tenant_id'))
    except (Tenant.DoesNotExist, ValueError, TypeError):
        return JsonResponse({'status': 'error', 'error': 'tenant not found'}, status=404)
    name = (data.get('name') or '').strip()
    subdomain = _clean_subdomain(data.get('subdomain'))
    if not name or not subdomain:
        return JsonResponse({'status': 'error', 'error': 'name and subdomain required'}, status=400)
    if Tenant.objects.filter(subdomain=subdomain).exclude(pk=tenant.pk).exists():
        return JsonResponse({'status': 'error', 'error': 'subdomain already taken'}, status=400)
    tenant.name = name
    tenant.subdomain = subdomain
    tenant.save(update_fields=['name', 'subdomain'])
    return JsonResponse({'status': 'ok'})

@superuser_and_reauthed
def tenant_delete(request):
    """Delete a tenant and everything belonging to it.

    Every tenant-scoped model has ``on_delete=CASCADE`` on its tenant FK, and so does
    Membership, so one delete takes the players, seating, hands, sheets, published
    rounds, schedule, screens, modes, ceremony state, publish target, settings and
    memberships with it. The post_delete signal drops the tenant cache.

    The user accounts themselves are left alone on purpose: an account may belong to
    another tournament, and tidying up one that no longer belongs anywhere is a
    separate decision rather than a side effect of this one.

    Guards, in order of how bad the mistake would be: the caller must retype the
    subdomain (this is the most destructive button in the app, so it borrows the
    database-restore page's typed-confirmation habit); the default tenant can never go,
    since every tenant FK defaults to it; and neither can the tenant the caller is
    currently working in, which would delete the ground they are standing on.
    """
    blocked = _standalone_blocked()
    if blocked is not None:
        return blocked
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    data = json_body(request)
    try:
        tenant = Tenant.objects.get(pk=data.get('tenant_id'))
    except (Tenant.DoesNotExist, ValueError, TypeError):
        return JsonResponse({'status': 'error', 'error': 'tenant not found'}, status=404)

    if _clean_subdomain(data.get('confirm')) != tenant.subdomain:
        return JsonResponse(
            {'status': 'error',
             'error': f'Type the subdomain "{tenant.subdomain}" to confirm.'}, status=400)

    if tenant.subdomain == Tenant.DEFAULT_SUBDOMAIN:
        return JsonResponse(
            {'status': 'error',
             'error': f'The "{Tenant.DEFAULT_SUBDOMAIN}" tenant is the fallback every '
                      'record points at and cannot be deleted.'}, status=400)

    current = get_tenant(request)
    if current is not None and current.pk == tenant.pk:
        return JsonResponse(
            {'status': 'error',
             'error': 'That is the tournament you are working in. Open another '
                      "tenant's admin and delete it from there."}, status=400)

    # Django leaves FileField files on disk, so the uploaded logo has to go by hand
    # or it outlives the tournament it belonged to.
    for row in TournamentSettings.objects.filter(tenant=tenant):
        if row.logo:
            row.logo.delete(save=False)

    subdomain, name = tenant.subdomain, tenant.name
    tenant.delete()
    return JsonResponse({'status': 'ok', 'subdomain': subdomain, 'name': name})


# A tenant's first admin is seeded through that tenant's own User-management page:
# a superuser bypasses membership, so they can open <subdomain>/admin?page=users
# and "Add a user" with the Admin role there — no separate seed endpoint needed.
