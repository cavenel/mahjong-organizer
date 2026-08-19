"""Drop four write-only timestamp columns.

`ScoreSheet.updated_at`, `CeremonyState.updated_at`, `PublishedRound.published_at`
(all auto_now) and `Screen.last_refresh` (auto_now_add, left over from screen
heartbeat scaffolding) were written on every save and never read: no view,
template, ordering, `latest()` or serialiser touched them.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('mahj', '0012_player_real_names'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='ceremonystate',
            name='updated_at',
        ),
        migrations.RemoveField(
            model_name='publishedround',
            name='published_at',
        ),
        migrations.RemoveField(
            model_name='scoresheet',
            name='updated_at',
        ),
        migrations.RemoveField(
            model_name='screen',
            name='last_refresh',
        ),
    ]
