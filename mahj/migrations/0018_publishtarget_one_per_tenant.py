import logging

from django.db import migrations, models

logger = logging.getLogger(__name__)


def ids_to_keep(rows):
    """The lowest id per tenant, from `(id, tenant_id)` pairs in ascending id order.

    Deliberately a copy of 0017's helper rather than an import: a migration is a
    historical record and must keep working regardless of what later code does, and a
    module whose name starts with a digit cannot be imported by name anyway.
    """
    keep, seen = set(), set()
    for pk, tenant_id in rows:
        if tenant_id in seen:
            continue
        seen.add(tenant_id)
        keep.add(pk)
    return keep


def drop_duplicate_targets(apps, schema_editor):
    """Keep the lowest-id PublishTarget per tenant, delete the rest.

    Mirrors 0017. Duplicates can only come from the race this migration's constraint
    closes — two concurrent saves each inserting a row — so this repairs rather than
    stopping the deploy. Which row was being read was already arbitrary: the three
    resolution sites use ``.order_by('id').first()``, so the lowest id is the one that
    was winning, and keeping it preserves the behaviour the tenant already had.

    Logged loudly, because a discarded row holds a host, a path and encrypted
    credentials someone configured. A clean database makes this a no-op.
    """
    PublishTarget = apps.get_model('mahj', 'PublishTarget')
    keep = ids_to_keep(
        PublishTarget.objects.order_by('id').values_list('id', 'tenant_id'))
    extras = PublishTarget.objects.exclude(id__in=keep)
    doomed = list(extras.values_list('id', 'tenant_id'))
    if doomed:
        logger.warning(
            "Dropping %d duplicate PublishTarget row(s) so one per tenant can be "
            "enforced; kept the lowest id for each tenant, which is the one the "
            "resolution sites were already selecting. Discarded (id, tenant): %s",
            len(doomed), doomed)
        extras.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('mahj', '0017_tournamentsettings_one_per_tenant'),
    ]

    operations = [
        migrations.RunPython(
            drop_duplicate_targets, migrations.RunPython.noop, elidable=True),
        migrations.AddConstraint(
            model_name='publishtarget',
            constraint=models.UniqueConstraint(
                fields=['tenant'], name='unique_publish_target_per_tenant'),
        ),
    ]
