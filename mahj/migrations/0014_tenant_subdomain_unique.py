from django.db import migrations, models


def check_no_duplicate_subdomains(apps, schema_editor):
    """Fail with an actionable message if the constraint below can't be applied.

    Migrations auto-apply on container start, so a bare IntegrityError here would
    stop a deploy with nothing to act on. Renaming the duplicates automatically
    isn't an option either — a subdomain is a live URL, and silently re-keying
    someone's site is worse than stopping. So name them and let the operator
    choose. A clean database makes this a no-op.
    """
    Tenant = apps.get_model('mahj', 'Tenant')
    seen, duplicated = set(), set()
    for subdomain in Tenant.objects.values_list('subdomain', flat=True):
        if subdomain in seen:
            duplicated.add(subdomain)
        seen.add(subdomain)
    if duplicated:
        raise RuntimeError(
            "Cannot make Tenant.subdomain unique — these subdomains are used by "
            "more than one tenant: {0}. Each tenant needs its own subdomain (it is "
            "the key every request resolves its tenant from). Rename or delete the "
            "extras, then redeploy.".format(', '.join(sorted(duplicated)))
        )


class Migration(migrations.Migration):

    dependencies = [
        ('mahj', '0013_drop_write_only_timestamps'),
    ]

    operations = [
        migrations.RunPython(
            check_no_duplicate_subdomains, migrations.RunPython.noop, elidable=True),
        migrations.AddConstraint(
            model_name='tenant',
            constraint=models.UniqueConstraint(
                fields=['subdomain'], name='unique_tenant_subdomain'),
        ),
    ]
