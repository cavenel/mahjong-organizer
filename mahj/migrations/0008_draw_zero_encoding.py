from django.db import migrations


def null_draws_to_zero(apps, schema_editor):
    """Reclassify surviving winner-less hands as explicit draws.

    ``Hand.win_by`` used to be NULL for a draw; now NULL means an unplayed
    placeholder row and 0 means a played draw. On a *validated* sheet the old
    validate step already pruned trailing unplayed rows, so every remaining
    NULL row is in fact a draw -> set it to 0. Unvalidated sheets still hold
    genuine NULL placeholders (all 16 rows), so they're left untouched.
    """
    ScoreSheet = apps.get_model('mahj', 'ScoreSheet')
    Hand = apps.get_model('mahj', 'Hand')
    for sheet in ScoreSheet.objects.filter(validated=True).iterator():
        Hand.objects.filter(
            tenant_id=sheet.tenant_id, round_nb=sheet.round_nb,
            table_nb=sheet.table_nb, win_by__isnull=True,
        ).update(win_by=0, win_from=None, points=0)


def zero_draws_to_null(apps, schema_editor):
    """Reverse: turn explicit draws (win_by 0) back into NULL winner-less rows."""
    Hand = apps.get_model('mahj', 'Hand')
    Hand.objects.filter(win_by=0).update(win_by=None)


class Migration(migrations.Migration):

    dependencies = [
        ('mahj', '0007_welcome_plain_text'),
    ]

    operations = [
        migrations.RunPython(null_draws_to_zero, zero_draws_to_null),
    ]
