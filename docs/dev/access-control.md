# Access control (developer notes)

The `Membership` join, the three tiers and cross-tenant isolation are documented
in [`data-model.md`](data-model.md#access-control-user--tenant-membership). This
file covers how a view gates on them.

## Helpers and decorators that enforce a gate

Every check is evaluated against the **current subdomain's** tenant, so a user's
access on one tenant says nothing about another. Two helpers in
`mahj/views/helpers.py` carry this:

- `current_membership(request)` returns the `Membership` for `(request.user,
  get_tenant(request))`, memoized on the request like `get_tenant` and
  `get_variables`. Superusers get a synthetic all-true result and need no row.
- `has_role(request, *roles)` is true for a superuser, for `is_tenant_admin`, or
  for any of the named tier-3 roles, always for the request's tenant.
  `is_publisher` also satisfies `'scorer'`. The publisher outranks the scorer,
  sees every unpublished score, and locks and reopens rounds, so score edits are
  theirs too. `has_role` applies that implication at call time. The row stores no
  such flag.

Use these decorators on every view. Do not re-derive the check inline.

- `@superuser_required` is platform ops only: tenant CRUD. Per-tenant
  backup/restore is a *tenant admin* action (`backup_admin.py`).
- `@tenant_admin_required` admits a superuser or `is_tenant_admin` for this
  tenant.
- `@tenant_role_required('scorer', 'display_op', ...)` admits a superuser, a
  tenant admin, or any listed role for this tenant.
- `@tenant_admin_and_reauthed` (`user_admin.py`) is `tenant_admin_required` plus a
  recent password re-confirmation. See below.

An anonymous user gets the usual login redirect. An authenticated user with no
membership for this subdomain, including a member of a *different* tenant, gets
no access (public / 403). That 403 for the wrong tenant is the isolation working
as intended.

## Django `is_staff` grants nothing, `is_superuser` is the platform operator

`/admin_db/` mounts unscoped models on every subdomain, so the admin site itself
requires `is_superuser` (`mahj/admin_site.py`, wired in through
`MahjAdminConfig.default_site`) instead of Django's default `is_staff`. The staff
flag therefore grants nothing anywhere. A stale `is_staff=True` on an old account
is inert, and existing rows were left alone rather than migrated. App-level
"tenant admin" is the `is_tenant_admin` membership flag. Never use the Django
staff flag for it. Don't reintroduce an `is_staff` predicate in a view path:
`test_invariants.py::test_no_access_decision_reads_the_staff_flag` fails if one
appears outside migration `0010`, which reads it only to convert it away.

## A tenant admin can manage credentials only for users confined to that tenant

A tenant admin may add or remove a user's membership *in their own tenant*
freely. Minting a login link, rotating credentials (revoking sesame links) and
deleting the account are allowed only when the target's memberships are entirely
within that tenant. A user shared across tenants is credential-managed only by a
superuser, which bounds the blast radius of a shared account. For such a user the
console offers only "remove from this tenant", which drops the membership and
keeps the account.

Minting counts as credential management even though it looks read-only. The link
is a full credential for the *account*, and it is returned to the admin who asked
for it. Opened on another tenant's subdomain it carries whatever roles the
account holds there, so membership gating alone doesn't contain it.

## Last-admin and self guards

You can't drop your own admin role, delete yourself, or delete the last admin *of
a tenant*. Superusers are exempt, so they can always recover a tenant.

## Re-auth: user management and the tournament reset need a fresh password

User-management pages and the destructive tournament reset require a recent
password re-confirmation on top of the role gate (`REAUTH_SESSION_KEY`,
`reauth_ok`, `tenant_admin_and_reauthed` in `user_admin.py`). A stale or borrowed
session can't drive them directly.

## Setup and Run workspaces do not decide access

The console sidebar is split into *Setup* and *Run* (`_AdminPage.area` in
`admin_views.py`), and the Setup workspace shows an in-progress banner once play
has begun (`scoring.tournament_in_progress`). Both are presentation. Every page
is still admitted by its `gate` alone. A Setup page requested by a non-admin
renders the same empty panel as an unknown page, with the Run sidebar around it,
so the shell never lists pages the account can't open.

## Sesame links are global auth, still gated by Membership

A magic link mints a Django login. Access is still decided by `Membership`, so a
link for tenant A used on tenant B's subdomain yields no access. There is no
special handling at link-minting time.

## The shell HTML cache is bucketed per role variant

The public/admin shell caches per role-variant (`desktop_html:{subdomain}:{view}`).
When changing how the `view` / `is_admin` token is computed, keep the
anon/privileged split intact so a privileged menu never lands in the `anon`
bucket.

## WebSocket consumers are unauthenticated relays

`mahj/consumers.py` holds broadcast relays keyed by subdomain, with no role gate.
Every sensitive fetch is an HTTP view. Don't assume a WS message is authorized.

## Migration and bootstrap of memberships

On a non-fresh single-tenant DB, the data migration maps each retired staff/group
user to a `Membership` (`is_staff` becomes `is_tenant_admin`). Multi-tenant DBs
can't be guessed, so they no-op with a warning. Those need
`manage.py assign_membership`, which
[deployment.md](../hosting/deployment.md#first-run) covers.

## When adding a view

A missed gate is a privilege leak. Confirm no `is_staff` or legacy-group
predicate survives in a view path. Pick the narrowest decorator that fits. If the
view feeds the shell menu, verify the role-variant cache split still holds.
