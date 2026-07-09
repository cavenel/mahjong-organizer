"""The on-screen message (TournamentSettings.welcome) is now stored as plain
text with real newlines; the display templates render it escaped with
``white-space: pre-line``. Older rows hold literal ``<br>`` tags (the admin
editor used to convert newlines to HTML on save) — convert them once."""
import re

from django.db import migrations

_BR = re.compile(r'<br\s*/?>', re.IGNORECASE)


def br_to_newlines(apps, schema_editor):
    TournamentSettings = apps.get_model('mahj', 'TournamentSettings')
    for row in TournamentSettings.objects.exclude(welcome=''):
        converted = _BR.sub('\n', row.welcome)
        if converted != row.welcome:
            row.welcome = converted
            row.save(update_fields=['welcome'])


class Migration(migrations.Migration):

    dependencies = [
        ('mahj', '0006_schedule_is_round'),
    ]

    operations = [
        migrations.RunPython(br_to_newlines, migrations.RunPython.noop),
    ]
