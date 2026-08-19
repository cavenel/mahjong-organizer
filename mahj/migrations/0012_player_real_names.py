from django.db import migrations, models


def backfill_real_names(apps, schema_editor):
    """Split the existing full_name into the new raw first/last fields, matching
    the old last_name()/first_name split. short_name keeps the disambiguated
    token that was renamed from first_name."""
    Player = apps.get_model('mahj', 'Player')
    for player in Player.objects.all().iterator():
        parts = player.full_name.split(" ")
        Player.objects.filter(pk=player.pk).update(
            first_name=parts[0],
            last_name=" ".join(parts[1:]),
        )


class Migration(migrations.Migration):

    dependencies = [
        ('mahj', '0011_remove_player_email'),
    ]

    operations = [
        # The field named first_name actually held the disambiguated short token.
        migrations.RenameField(
            model_name='player',
            old_name='first_name',
            new_name='short_name',
        ),
        # first_name/last_name now hold the person's real name, raw from import.
        migrations.AddField(
            model_name='player',
            name='first_name',
            field=models.CharField(default='', max_length=70),
        ),
        migrations.AddField(
            model_name='player',
            name='last_name',
            field=models.CharField(blank=True, default='', max_length=70),
        ),
        migrations.RunPython(backfill_real_names, migrations.RunPython.noop),
    ]
