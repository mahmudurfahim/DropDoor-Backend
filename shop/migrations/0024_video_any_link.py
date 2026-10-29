from django.db import migrations, models
import shop.models


class Migration(migrations.Migration):
    dependencies = [("shop", "0023_referrals")]

    operations = [
        migrations.AlterField(
            model_name="product", name="video_url",
            field=models.URLField(blank=True, max_length=500, validators=[shop.models.video_ok]),
        ),
    ]
