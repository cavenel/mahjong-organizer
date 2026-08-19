# Access control (developer notes)

How per-tenant authorization is enforced in the view layer. The *model* — the
`Membership` join, the three tiers, and cross-tenant isolation — is documented in
[`docs/data-model.md`](../data-model.md#access-control-user--tenant-membership);
this file covers how a view actually gates on it and the non-obvious rules a
maintainer needs before touching a gate.

## Enforcing a gate

Every check is evaluated against the **current subdomain's** tenant, so a user's
access on one tenant says nothing about another. Two helpers and a small set of
decorators in `mahj/views/helpers.py` carry this:

- `current_membership(request)` — the `Membership` for `(request.user,
  get_tenant(request))`, memoized on the request (like `get_tenant` /
  `get_variables`). Superusers get a synthetic all-true result and need no row.
- `has_role(request, *roles)` — superuser OR `is_tenant_admin` OR any of the named
  tier-3 roles, for the request's tenant.

Decorators (use these on every view; do not re-derive the check inline):

- `@superuser_required` — platform ops only: tenant CRUD and the whole-cluster DB
  restore (`restore_admin.py`).
- `@tenant_admin_required` — superuser OR `is_tenant_admin` for this tenant.
- `@tenant_role_required('scorer', 'display_op', ...)` — superuser OR tenant admin
  OR any listed role for this tenant.
- `@tenant_admin_and_reauthed` (`user_admin.py`) — `tenant_admin_required` **plus**
  a recent password re-confirmation; see *Re-auth* below.

Access outcomes: anonymous → the usual login redirect; authenticated but with no
membership for this subdomain (including a member of a *different* tenant) → no
access (public / 403). That 403-for-the-wrong-tenant is the isolation, not a bug.

## Rules that aren't obvious from the code

- **Django `is_staff` / `is_superuser` are platform-only.** `is_superuser` is the
  cross-tenant platform operator; `is_staff` is reserved for the Django admin site
  and grants **no** app access. App-level "tenant admin" is the `is_tenant_admin`
  membership flag — never the Django staff flag. Don't reintroduce an `is_staff`
  predicate in a view path.
- **Credential containment.** A tenant admin may add/remove a user's membership
  *in their own tenant* freely, but may only **mint** a login link, rotate
  credentials (revoke sesame links) or delete the account when the target's
  memberships are entirely within that tenant. A user shared across tenants is
  credential-managed only by a superuser — this bounds the blast radius of a shared
  account. The console otherwise offers only "remove from this tenant" (drop the
  membership, keep the account).

  Minting counts as credential management even though it looks read-only: the link
  is a full credential for the *account* and it is returned to the admin who asked
  for it. Opened on another tenant's subdomain it carries whatever roles the
  account holds there, so membership gating alone doesn't contain it.
- **Last-admin / self guards.** You can't drop your own admin role, delete
  yourself, or delete the last admin *of a tenant*. Superusers are exempt (they
  can always recover a tenant).
- **Re-auth ("sudo mode").** User-management and the destructive tournament reset
  require a recent password re-confirmation (`REAUTH_SESSION_KEY`, `reauth_ok`,
  `tenant_admin_and_reauthed` in `user_admin.py`) on top of the role gate, so a
  stale or borrowed session can't drive them directly.

## Operational notes

- **Sesame links are global auth but membership-gated.** A magic link mints a
  Django login, but access is still decided by `Membership`, so a link for
  tenant A used on tenant B's subdomain yields no access — no special handling at
  link-minting time.
- **Role-variant HTML cache.** The public/admin shell caches per role-variant
  (`desktop_html:{subdomain}:{view}`). When changing how the `view` / `is_admin`
  token is computed, keep the anon/privileged split intact so a privileged menu
  never lands in the `anon` bucket.
- **WebSocket consumers are unauthenticated relays.** `mahj/consumers.py` are
  broadcast relays keyed by subdomain and carry no role gate — every sensitive
  fetch is an HTTP view. Don't assume a WS message is authorized.
- **Migration / bootstrap.** On a non-fresh single-tenant DB, the data migration
  maps each retired staff/group user to a `Membership` (`is_staff` →
  `is_tenant_admin`); multi-tenant DBs can't be guessed and no-op with a warning —
  use `manage.py assign_membership <user> <subdomain> --roles=...`. The standalone
  build's first-run bootstrap grants the operator account a tenant-admin
  membership.

## When adding a view

A missed gate is a privilege leak. Confirm no `is_staff`/legacy-group predicate
survives in a view path, pick the narrowest decorator that fits, and if the view
feeds the shell menu, verify the role-variant cache split still holds.
