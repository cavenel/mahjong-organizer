from django.db import migrations, models


def set_has_teams_from_roster(apps, schema_editor):
    """Preserve the previous derived behaviour on existing data: a tournament
    that already has any teamed player becomes an explicit team tournament."""
    TournamentSettings = apps.get_model('mahj', 'TournamentSettings')
    Player = apps.get_model('mahj', 'Player')
    teamed_tenants = set(
        Player.objects.exclude(team='').values_list('tenant_id', flat=True)
    )
    TournamentSettings.objects.filter(tenant_id__in=teamed_tenants).update(has_teams=True)


class Migration(migrations.Migration):

    dependencies = [
        ('mahj', '0002_tournamentsettings_countrycourt_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='tournamentsettings',
            name='has_teams',
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name='tournamentsettings',
            name='rules',
            field=models.CharField(default='MCR', max_length=70),
        ),
        migrations.RunPython(set_has_teams_from_roster, migrations.RunPython.noop),
    ]
