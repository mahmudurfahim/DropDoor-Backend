import django.contrib.auth.models
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("shop", "0021_activity_logs"),
    ]

    operations = [
        migrations.CreateModel(
            name="AdminAccount",
            fields=[],
            options={"verbose_name": "admin account", "verbose_name_plural": "admin accounts", "proxy": True, "indexes": [], "constraints": []},
            bases=("shop.user",),
            managers=[("objects", django.contrib.auth.models.UserManager())],
        ),
    ]
