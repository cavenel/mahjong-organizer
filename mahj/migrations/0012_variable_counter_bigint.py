from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('SOMMC2018', '0011_variable_counter'),
    ]

    operations = [
        migrations.AlterField(
            model_name='variable',
            name='counter',
            field=models.BigIntegerField(default=-1),
        ),
    ]
