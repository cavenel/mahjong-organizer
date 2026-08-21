import logging

from django.db import migrations, models

logger = logging.getLogger(__name__)


def ids_to_keep(rows):
    """The lowest id per tenant, from `(id, tenant_id)` pairs in ascending id order.

    Split out from the migration below so the decision it makes is testable: once
    the constraint exists, its input can no longer be created, and sqlite bakes the
    constraint into the table so it can't be lifted for a test either.
    """
    keep, seen = set(), set()
    for pk, tenant_id in rows:
        if tenant_id in seen:
            continue
        seen.add(tenant_id)
        keep.add(pk)
    return keep


def drop_duplicate_settings(apps, schema_editor):
    """Keep the lowest-id TournamentSettings row per tenant, delete the rest.

    Unlike the duplicate-subdomain check in 0014, this one repairs rather than
    stops: a duplicate here is the product of a race two workers could lose on a
    fresh tenant (both saw no row, both provisioned one), so it is the app's bug to
    clean up, not a decision for the operator. Which row was being read was already
    arbitrary — `.first()` with no ordering — so keeping the lowest id is at least
    deterministic. Logged loudly, because a discarded row could have held branding
    someone typed. A clean database makes this a no-op.
    """
    TournamentSettings = apps.get_model('mahj', 'TournamentSettings')
    keep = ids_to_keep(
        TournamentSettings.objects.order_by('id').values_list('id', 'tenant_id'))
    extras = TournamentSettings.objects.exclude(id__in=keep)
    doomed = list(extras.values_list('id', 'tenant_id'))
    if doomed:
        logger.warning(
            "Dropping %d duplicate TournamentSettings row(s) so one per tenant can "
            "be enforced; kept the lowest id for each tenant. Discarded (id, tenant): %s",
            len(doomed), doomed)
        extras.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('mahj', '0016_publishtarget_backup_path'),
    ]

    operations = [
        migrations.RunPython(
            drop_duplicate_settings, migrations.RunPython.noop, elidable=True),
        migrations.AddConstraint(
            model_name='tournamentsettings',
            constraint=models.UniqueConstraint(
                fields=['tenant'], name='unique_settings_per_tenant'),
        ),
    ]
