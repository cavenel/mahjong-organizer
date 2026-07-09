"""Staff-only user management: create accounts, toggle roles, and mint/revoke
passwordless login links (django-sesame).

Links are stateless sesame tokens, so two constraints shape this module:
  * validity is the single global ``SESAME_MAX_AGE`` setting (no per-link TTL);
  * "revoking" a user's links rotates their password hash, which invalidates
    every link that user holds at once (per-user, not per-link).
"""

import json
import time
from functools import wraps

from django.contrib.auth.decorators import user_passes_test
from django.contrib.auth.models import Group, User
from django.http import JsonResponse
from sesame.utils import get_token

# The three role groups the console manages. Staff (is_staff) is handled
# separately as a flag, not a group.
ROLE_GROUPS = ['Scorer', 'Display_op', 'Publisher']

staff_only = user_passes_test(lambda u: u.is_staff)
superuser_only = user_passes_test(lambda u: u.is_superuser)

# "Sudo mode": staff must re-enter their password to reach user management, so an
# unattended/borrowed admin session can't be used to create or steal accounts.
# The confirmation is stamped in the session and only lasts this long.
REAUTH_SESSION_KEY = 'users_reauth_at'
USERS_REAUTH_MAX_AGE = 600  # seconds


def reauth_ok(request):
    """True if this session has confirmed the staffer's password within the last
    ``USERS_REAUTH_MAX_AGE`` seconds. Link-only accounts (no usable password)
    have nothing to confirm, so they can never satisfy this and are kept out of
    user management entirely."""
    user = request.user
    if not user.has_usable_password():
        return False
    ts = request.session.get(REAUTH_SESSION_KEY)
    return bool(ts) and (time.time() - ts) < USERS_REAUTH_MAX_AGE


def staff_and_reauthed(view):
    """Like ``staff_only``, but also requires a recent password re-confirmation.
    Mutating endpoints carry this so they can't be driven directly (bypassing
    the page-level gate) from a stale session. Anonymous/non-staff still get the
    usual login redirect; an authenticated staffer whose confirmation has lapsed
    gets a JSON ``reauth_required`` the front-end can act on."""
    @wraps(view)
    def inner(request, *args, **kwargs):
        if not reauth_ok(request):
            return JsonResponse({'status': 'reauth_required'}, status=403)
        return view(request, *args, **kwargs)
    return staff_only(inner)


def superuser_and_reauthed(view):
    """Like ``staff_and_reauthed``, but requires ``is_superuser``.

    Restoring the database is a platform-operator action: the dump/restore is
    whole-cluster (it replaces every tenant's rows at once), so it must never be
    reachable by a per-tenant staff admin. Django users are not tenant-scoped, so
    the staff flag is no cross-tenant guard — the superuser flag is."""
    @wraps(view)
    def inner(request, *args, **kwargs):
        if not reauth_ok(request):
            return JsonResponse({'status': 'reauth_required'}, status=403)
        return view(request, *args, **kwargs)
    return superuser_only(inner)


@staff_only
def user_reauth(request):
    """Confirm the current staff user's password and stamp the session."""
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    data = _body(request)
    if data is None:
        return JsonResponse({'status': 'bad_request'}, status=400)
    if not request.user.check_password(data.get('password') or ''):
        return JsonResponse({'status': 'error', 'error': 'incorrect password'}, status=403)
    request.session[REAUTH_SESSION_KEY] = time.time()
    return JsonResponse({'status': 'ok'})


def _body(request):
    try:
        return json.loads(request.body) if request.body else {}
    except ValueError:
        return None


def _set_roles(user, roles):
    """Make the user's group membership exactly ``roles`` (restricted to the
    known role groups; unknown names are ignored)."""
    wanted = [r for r in roles if r in ROLE_GROUPS]
    for name in ROLE_GROUPS:
        group, _ = Group.objects.get_or_create(name=name)
        if name in wanted:
            user.groups.add(group)
        else:
            user.groups.remove(group)


@staff_and_reauthed
def user_create(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    data = _body(request)
    if data is None:
        return JsonResponse({'status': 'bad_request'}, status=400)

    username = (data.get('username') or '').strip()
    if not username:
        return JsonResponse({'status': 'error', 'error': 'username required'}, status=400)
    if User.objects.filter(username=username).exists():
        return JsonResponse({'status': 'error', 'error': 'username already taken'}, status=400)

    user = User(username=username, is_staff=bool(data.get('is_staff')))
    password = data.get('password') or ''
    if password:
        user.set_password(password)
    else:
        # Passwordless: the account can only log in via a sesame link.
        user.set_unusable_password()
    user.save()
    _set_roles(user, data.get('roles', []))

    return JsonResponse({'status': 'ok', 'id': user.id, 'username': user.username})


@staff_and_reauthed
def user_update_roles(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    data = _body(request)
    if data is None:
        return JsonResponse({'status': 'bad_request'}, status=400)

    try:
        user = User.objects.get(pk=data.get('user_id'))
    except (User.DoesNotExist, ValueError, TypeError):
        return JsonResponse({'status': 'error', 'error': 'user not found'}, status=404)

    is_staff = bool(data.get('is_staff'))
    # Guard: don't let staff strip their own staff flag and lock themselves out.
    if user.id == request.user.id and not is_staff:
        return JsonResponse({'status': 'error', 'error': 'you cannot remove your own staff access'}, status=400)

    user.is_staff = is_staff
    user.save(update_fields=['is_staff'])
    _set_roles(user, data.get('roles', []))

    return JsonResponse({'status': 'ok'})


@staff_and_reauthed
def user_generate_link(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    data = _body(request)
    if data is None:
        return JsonResponse({'status': 'bad_request'}, status=400)

    try:
        user = User.objects.get(pk=data.get('user_id'))
    except (User.DoesNotExist, ValueError, TypeError):
        return JsonResponse({'status': 'error', 'error': 'user not found'}, status=404)

    # Land the kiosk straight on the scoring page; the sesame middleware logs the
    # user in from the token on the way through. token chars are URL-safe.
    url = request.build_absolute_uri('/admin?page=scoring&sesame=' + get_token(user))
    return JsonResponse({'status': 'ok', 'url': url})


@staff_and_reauthed
def user_revoke_links(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    data = _body(request)
    if data is None:
        return JsonResponse({'status': 'bad_request'}, status=400)

    try:
        user = User.objects.get(pk=data.get('user_id'))
    except (User.DoesNotExist, ValueError, TypeError):
        return JsonResponse({'status': 'error', 'error': 'user not found'}, status=404)

    # Rotating the password hash invalidates every existing sesame token for this
    # user. It also clears any usable password, so the account becomes link-only.
    user.set_unusable_password()
    user.save(update_fields=['password'])
    return JsonResponse({'status': 'ok'})


@staff_and_reauthed
def user_delete(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    data = _body(request)
    if data is None:
        return JsonResponse({'status': 'bad_request'}, status=400)

    try:
        user = User.objects.get(pk=data.get('user_id'))
    except (User.DoesNotExist, ValueError, TypeError):
        return JsonResponse({'status': 'error', 'error': 'user not found'}, status=404)

    if user.id == request.user.id:
        return JsonResponse({'status': 'error', 'error': 'you cannot delete your own account'}, status=400)
    if user.is_staff and User.objects.filter(is_staff=True).count() <= 1:
        return JsonResponse({'status': 'error', 'error': 'cannot delete the last staff account'}, status=400)

    user.delete()
    return JsonResponse({'status': 'ok'})
