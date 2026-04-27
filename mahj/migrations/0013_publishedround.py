from django.db import migrations, models
import django.db.models.deletion
import mahj.models


def migrate_final_to_publishedround(apps, schema_editor):
    """Port existing Variable.final state into PublishedRound rows.

    For each tenant with a Variable row, if final > 0 and the last round
    is fully scored, create a PublishedRound for the last round at
    reveal_level = final. Also backfill PublishedRound rows for every
    earlier fully-scored round (reveal_level = 100).
    """
    Variable = apps.get_model('SOMMC2018', 'Variable')
    Position = apps.get_model('SOMMC2018', 'Position')
    PublishedRound = apps.get_model('SOMMC2018', 'PublishedRound')

    for variables in Variable.objects.all():
        tenant_id = variables.tenant_id
        nb_rounds = variables.nb_rounds
        final = getattr(variables, 'final', 0) or 0

        # Find the highest fully-scored round (no null mp/tp).
        positions = list(Position.objects.filter(tenant_id=tenant_id))
        if not positions:
            continue
        incomplete_rounds = {
            p.round_nb for p in positions
            if p.minipoints is None or p.tablepoints is None
        }
        max_round = max((p.round_nb for p in positions), default=0)
        last_complete = 0
        for r in range(1, max_round + 1):
            if r in incomplete_rounds:
                break
            last_complete = r

        if last_complete <= 0:
            continue

        # Last round: preserve reveal state; earlier: fully visible.
        for r in range(1, last_complete + 1):
            if r == nb_rounds:
                reveal = final if final > 0 else 0
                PublishedRound.objects.update_or_create(
                    tenant_id=tenant_id, round_nb=r,
                    defaults={'reveal_level': reveal},
                )
            else:
                PublishedRound.objects.update_or_create(
                    tenant_id=tenant_id, round_nb=r,
                    defaults={'reveal_level': 100},
                )


def reverse_noop(apps, schema_editor):
    PublishedRound = apps.get_model('SOMMC2018', 'PublishedRound')
    PublishedRound.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('SOMMC2018', '0012_variable_counter_bigint'),
    ]

    operations = [
        migrations.CreateModel(
            name='PublishedRound',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('round_nb', models.IntegerField()),
                ('reveal_level', models.IntegerField(default=100)),
                ('published_at', models.DateTimeField(auto_now=True)),
                ('tenant', models.ForeignKey(
                    default=mahj.models.Tenant.get_default_pk,
                    on_delete=django.db.models.deletion.CASCADE,
                    to='SOMMC2018.tenant',
                )),
            ],
            options={
                'unique_together': {('tenant', 'round_nb')},
            },
        ),
        migrations.RunPython(migrate_final_to_publishedround, reverse_noop),
        migrations.RemoveField(
            model_name='Variable',
            name='final',
        ),
    ]
