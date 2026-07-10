# Plan — per-tenant user management

Add real per-tenant access control. Today tenancy exists only at the data layer
(every row is scoped to a `Tenant`, resolved from the request **subdomain**),
while users are **global**: a `Scorer`/`Display_op`/`Publisher` group or the
Django `is_staff` flag grants that role on *every* tenant. See the caveats in
`mahj/views/user_admin.py:64-65` and `scripts/DB_RESTORE.md:40` — this plan
removes them.

## Target model

Three tiers:

1. **Superuser** — platform operator. Cross-tenant. Creates/renames tenants,
   seeds the first tenant admin, runs the whole-cluster DB restore. This is
   Django `is_superuser`; nobody else is cross-tenant.
2. **Tenant admin** — full admin over the tenant(s) they belong to. Manages that
   tenant's roles (including promoting co-admins). Cannot create tenants, cannot
   reach the whole-cluster restore, cannot see other tenants.
3. **Tenant roles** — `Scorer` / `Display_op` / `Publisher`, scoped to one
   tenant, managed by that tenant's admin.

### Decisions taken

- **Multiple tenants per user** (join table, not a OneToOne profile). The runtime
  permission check is evaluated against the *current subdomain's* tenant either
  way, so multi-tenancy costs nothing in the hot path — it only lets one login
  (e.g. a federation organizer) admin several events without being a superuser.
  A single-tenant user is just the common case of one membership.
- **Tenant admins can grant co-admins** for a tenant they administer (superusers
  seed the first one).
- **Destructive-credential containment.** A tenant admin may add/remove a user's
  membership *in their own tenant* freely, but may only rotate credentials
  (revoke links) or delete the account for users whose memberships are entirely
  within that tenant. A user shared across tenants is credential-managed only by
  a superuser. This bounds the blast radius of shared accounts.
- **Decouple the app from Django `is_staff`.** `is_staff`/`is_superuser` stay
  reserved for the Django admin site / platform ops. App-level "tenant admin" is
  a membership flag, never the Django staff flag.

## Data model — `Membership`

New model in `mahj/models.py` (NOT a `TenantAwareModel` — it *defines* scope and
references `auth.User`):

```python
class Membership(models.Model):
    user   = models.ForeignKey(User, on_delete=models.CASCADE, related_name='memberships')
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='memberships')
    is_tenant_admin = models.BooleanField(default=False)   # tier 2
    is_scorer       = models.BooleanField(default=False)   # tier 3
    is_display_op   = models.BooleanField(default=False)
    is_publisher    = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'tenant'], name='unique_membership_per_tenant'),
        ]
```

Boolean flags mirror the existing group set exactly (queryable, no extra table).
`is_tenant_admin` implies all app roles for that tenant (matches today's "staff
implies scorer/display/publisher"). The Django `Group`s `Scorer`/`Display_op`/
`Publisher` are retired.

## Phase 1 — Model + migration

- Add `Membership` + migration.
- **Data migration** (best-effort, for any non-fresh install): if exactly one
  `Tenant` exists, create a `Membership` in it for every user carrying a
  retired group or `is_staff` (map `is_staff` → `is_tenant_admin`, each group →
  its flag). If multiple tenants exist it can't guess — no-op + emit a warning;
  a `manage.py assign_membership <user> <subdomain> --roles=...` command covers
  the manual case. Prod is a fresh DB (only a superuser + the imported tenant),
  so the superuser bypass carries it and the migration is effectively a no-op
  there.
- **Standalone build** (single `LOCAL_TENANT`): first-run bootstrap grants the
  operator account a tenant-admin membership (or it's a superuser — either
  works). Note in `docs/deployment.md`.

## Phase 2 — Permission core (`mahj/views/helpers.py`)

- `current_membership(request)` — memoized on `request._membership` like
  `get_tenant`/`get_variables`. Returns the `Membership` for
  `(request.user, get_tenant(request))` or `None`. Superusers get a synthetic
  all-true result (they bypass; no row required).
- Replace the global predicates. Today `is_scorer(user)` etc. take a bare user
  and OR in `is_staff`; they become request-aware:
  `has_role(request, 'scorer')` = superuser OR membership.is_tenant_admin OR
  membership.is_scorer. Keep thin back-compat wrappers only if a caller can't be
  converted cleanly.
- Decorators to replace the ad-hoc `@user_passes_test(...)`:
  - `@superuser_required` — platform ops (tenant CRUD, restore).
  - `@tenant_admin_required` — superuser OR `is_tenant_admin` for the request's
    tenant.
  - `@tenant_role_required('scorer', 'display_op', ...)` — superuser OR admin OR
    any listed role for the request's tenant.
  - Anonymous → the usual login redirect; authenticated-but-wrong-tenant (a
    member of a *different* tenant on this subdomain) → treated as no access
    (public / 403), which is exactly the isolation we want.

## Phase 3 — Rewire the gates (85 call sites, 7 view files)

`admin_views.py`, `display.py`, `print_views.py`, `ceremony.py`,
`score_entry.py`, `user_admin.py`, `restore_admin.py`.

- `@user_passes_test(lambda u: u.is_staff)` → `@tenant_admin_required`.
- `@user_passes_test(is_display_op)` → `@tenant_role_required('display_op')`,
  and likewise scorer/publisher.
- `restore_admin.py` stays `@superuser_required` (whole-cluster action).
- **Admin-shell nav + public menu.** `public.py:33,43-45,198` derive the shell
  `view` token and menu from `is_staff`/`can_access_admin(user)`. Recompute these
  from the tenant-aware helpers. The `desktop_html:{subdomain}:{view}` cache key
  logic is unchanged — only how `view`/`is_admin` are computed changes; keep the
  per-variant caching intact so a privileged menu never leaks into the `anon`
  bucket.
- **Out of scope:** the WebSocket consumers (`mahj/consumers.py`) are
  unauthenticated broadcast relays keyed by subdomain — they carry no role gate
  today (sensitive fetches are all HTTP views), so nothing to change there.

## Phase 4 — User-management console (`mahj/views/user_admin.py` + templates)

Keep the "sudo mode" reauth (`REAUTH_SESSION_KEY`, `staff_and_reauthed`) — it
still applies, re-scoped to `tenant_admin`.

**Superuser mode** (new, e.g. `page=tenants`):
- List / create / rename tenants (name + subdomain).
- Seed a tenant admin: create-or-select a user and give them an
  `is_tenant_admin` membership in a chosen tenant (needs a tenant selector since
  the superuser isn't tied to a subdomain).

**Tenant-admin mode** (`page=users`, scoped to `get_tenant(request)`):
- List only memberships whose `tenant == get_tenant(request)`. Other tenants'
  users are invisible.
- Create user → create the user (global-unique username; password or
  passwordless sesame) **and** a `Membership` in this tenant with chosen roles
  (co-admins allowed). Username already taken → reject (don't leak which tenant).
- Edit roles, generate/revoke sesame links, delete — subject to the
  **containment rule**: revoke-links/delete only permitted when the target's
  memberships ⊆ {this tenant}; otherwise offer only "remove from this tenant"
  (delete the membership, keep the account). Sesame links are global auth but
  gated by membership, so a link for tenant-A on tenant-B's subdomain yields no
  access — no change needed to link minting.
- Keep existing guards: can't drop your own admin, can't delete self, can't
  delete the last admin **of this tenant**.

## Phase 5 — Tests + docs

- `mahj/tests/` currently seeds global staff/groups (`test_security.py`,
  `test_admin_shell_nav.py`, `test_display_admin.py`, `test_publisher_overview.py`,
  `test_reset.py`, `test_restore_admin.py`, `test_scoring_rules.py`,
  `test_scan.py`). Update the fixtures to create `Membership` rows.
- New tests: cross-tenant isolation (a scorer/admin of tenant A gets 403 on
  tenant B), superuser bypass, the containment rule (tenant admin can't nuke a
  shared account), tenant-admin console scoping, and the data migration.
- Docs: add `Membership` to `docs/data-model.md`; remove the "users are not
  tenant-scoped" caveats in `user_admin.py:64-65` and `DB_RESTORE.md:40`; note
  the standalone/bootstrap behaviour in `docs/deployment.md`.

## Risk notes

- Biggest surface is Phase 3's 85 gates + the shell `view`/menu token — a missed
  gate is a privilege leak, so it warrants a grep-driven audit that no
  `is_staff`/group predicate survives in a view path.
- Username uniqueness stays global (Django constraint); the console must handle
  collisions gracefully rather than 500.
- The public leaderboard HTML cache is keyed per role-variant — verify the
  tenant-aware `view` computation preserves the anon/privileged split.
