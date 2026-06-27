from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('SOMMC2018', '0022_variable_logo_variable_logo_etag'),
    ]

    operations = [
        migrations.AddField(
            model_name='variable',
            name='rotation_time',
            field=models.IntegerField(default=10),
        ),
    ]
