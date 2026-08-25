from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mahj', '0018_publishtarget_one_per_tenant'),
    ]

    operations = [
        migrations.AddField(
            model_name='tournamentsettings',
            name='is_test',
            field=models.BooleanField(default=False),
        ),
    ]
