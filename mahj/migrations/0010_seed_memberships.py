from django.db import migrations

# Retired Django Group -> Membership flag. Superuser/is_staff map separately.
GROUP_TO_FLAG = {
    'Scorer': 'is_scorer',
    'Display_op': 'is_display_op',
    'Publisher': 'is_publisher',
}


def seed_memberships(apps, schema_editor):
    """Best-effort migration of the old global roles onto per-tenant Memberships.

    Old model: roles were global (Django ``is_staff`` + the Scorer/Display_op/
    Publisher groups) and applied to every tenant. There is only one tenant to
    attribute them to when exactly one exists, so:

      - exactly one Tenant -> create a Membership in it for every user carrying a
        retired group or ``is_staff`` (``is_staff`` -> ``is_tenant_admin``, each
        group -> its flag). Superusers are skipped: they bypass membership.
      - zero or several Tenants -> can't guess which tenant a global role meant,
        so no-op and warn. Use ``manage.py assign_membership`` for those.

    Prod is a fresh DB (a superuser + one imported tenant), so the superuser
    bypass carries it and this is effectively a no-op there.
    """
    Tenant = apps.get_model('mahj', 'Tenant')
    Membership = apps.get_model('mahj', 'Membership')
    User = apps.get_model('auth', 'User')
    Group = apps.get_model('auth', 'Group')

    tenants = list(Tenant.objects.all())
    if len(tenants) != 1:
        if tenants:
            import warnings
            warnings.warn(
                f"seed_memberships: {len(tenants)} tenants exist; cannot attribute "
                "global roles to one tenant. No memberships created — use "
                "'manage.py assign_membership <user> <subdomain> --roles=...'.",
                stacklevel=2,
            )
        return

    tenant = tenants[0]
    group_ids = {name: gid for name, gid in
                 Group.objects.filter(name__in=GROUP_TO_FLAG).values_list('name', 'id')}

    for user in User.objects.all():
        if user.is_superuser:
            continue  # superusers bypass membership entirely
        flags = {'is_tenant_admin': bool(user.is_staff)}
        user_group_ids = set(user.groups.values_list('id', flat=True))
        for name, flag in GROUP_TO_FLAG.items():
            flags[flag] = group_ids.get(name) in user_group_ids
        if not any(flags.values()):
            continue  # a plain account with no old role gets no membership
        Membership.objects.get_or_create(user=user, tenant=tenant, defaults=flags)


class Migration(migrations.Migration):

    dependencies = [
        ('mahj', '0009_membership'),
    ]

    operations = [
        # Irreversible seed: reversing would delete rows a tenant admin may have
        # since edited. Rows are cheap to recreate by hand if a rollback is needed.
        # elidable: backfills rows that predate this migration; a fresh database
        # built from the squashed baseline has none, so the squash may drop it.
        migrations.RunPython(seed_memberships, migrations.RunPython.noop, elidable=True),
    ]
