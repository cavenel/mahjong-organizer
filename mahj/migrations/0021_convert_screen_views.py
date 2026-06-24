from django.db import migrations

# Old free-text view strings -> the new "scores:<density>:<page>" grammar.
LEGACY = {
    "": "black",
    "null": "black",
    "black": "black",
    "counter": "counter",
    "schedule": "schedule",
    "scores p. 1": "scores:detailed:1",
    "scores p. 2": "scores:detailed:2",
    "scores all": "scores:detailed:all",
    "scores all, total only": "scores:totals:all",
}


def _convert(view):
    """Map one stored view to the new grammar; leave already-valid values be."""
    if view in ("black", "counter", "schedule") or (view or "").startswith("scores:"):
        return view
    return LEGACY.get(view, "black")


def forwards(apps, schema_editor):
    import json

    Screen = apps.get_model("SOMMC2018", "Screen")
    for screen in Screen.objects.all():
        new = _convert(screen.view)
        if new != screen.view:
            screen.view = new
            screen.save(update_fields=["view"])

    ScreenMode = apps.get_model("SOMMC2018", "ScreenMode")
    for mode in ScreenMode.objects.all():
        try:
            views = json.loads(mode.views)
        except (ValueError, TypeError):
            continue
        converted = [_convert(v) for v in views]
        if converted != views:
            mode.views = json.dumps(converted)
            mode.save(update_fields=["views"])


class Migration(migrations.Migration):

    dependencies = [
        ("SOMMC2018", "0020_variable_total_columns"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
