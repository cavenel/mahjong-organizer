from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mahj', '0014_tenant_subdomain_unique'),
    ]

    operations = [
        migrations.AddField(
            model_name='seat',
            name='version',
            field=models.IntegerField(default=0),
        ),
    ]
