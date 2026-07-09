from django.db import migrations, models


def backfill_is_round(apps, schema_editor):
    """Mark existing schedule rows as rounds using the old name heuristic.

    Before ``is_round`` existed, a row counted as a playing round when its name
    contained "Round" or "Session" (see the pre-migration scoring code). Preserve
    that classification for already-imported tournaments.
    """
    Schedule = apps.get_model('mahj', 'Schedule')
    Schedule.objects.filter(name__icontains='round').update(is_round=True)
    Schedule.objects.filter(name__icontains='session').update(is_round=True)


class Migration(migrations.Migration):

    dependencies = [
        ('mahj', '0005_tournamentsettings_public_url'),
    ]

    operations = [
        migrations.AddField(
            model_name='schedule',
            name='is_round',
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(backfill_is_round, migrations.RunPython.noop),
    ]
