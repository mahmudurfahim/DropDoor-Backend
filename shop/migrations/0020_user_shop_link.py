from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shop", "0019_withdraw_balance_before"),
    ]

    operations = [
        migrations.AddField(model_name="user", name="shop_link", field=models.CharField(blank=True, max_length=300)),
    ]
