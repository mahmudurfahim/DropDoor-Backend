import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("admin", "0003_logentry_add_action_flag_choices"),
        ("shop", "0020_user_shop_link"),
    ]

    operations = [
        migrations.CreateModel(
            name="ResellerLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("phone", models.CharField(blank=True, max_length=15)),
                ("action", models.CharField(max_length=80)),
                ("method", models.CharField(max_length=7)),
                ("path", models.CharField(max_length=200)),
                ("status", models.PositiveSmallIntegerField()),
                ("detail", models.CharField(blank=True, max_length=300)),
                ("ip", models.CharField(blank=True, max_length=45)),
                ("created", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "reseller log", "verbose_name_plural": "reseller logs", "ordering": ["-id"]},
        ),
        migrations.CreateModel(
            name="AdminLog",
            fields=[],
            options={"verbose_name": "admin log", "verbose_name_plural": "admin logs", "proxy": True, "indexes": [], "constraints": []},
            bases=("admin.logentry",),
        ),
    ]
