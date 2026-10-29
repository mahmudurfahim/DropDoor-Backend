import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


def rename_codes(apps, schema_editor):
    """Order codes changed from ED1008 to DO1008: the ledger keeps the order code in `ref`, so the old rows follow the new name. Also add the referral_bonus setting row so it shows in Django admin."""
    Ledger = apps.get_model("shop", "Ledger")
    for row in Ledger.objects.filter(kind__in=["profit", "return_fee"], ref__startswith="ED"):
        row.ref = "DO" + row.ref[2:]
        row.save(update_fields=["ref"])
    Setting = apps.get_model("shop", "Setting")
    Setting.objects.get_or_create(key="referral_bonus", defaults={"value": getattr(settings, "REFERRAL_BONUS", 0)})


class Migration(migrations.Migration):
    dependencies = [("shop", "0022_adminaccount")]

    operations = [
        migrations.AddField(
            model_name="user", name="referred_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="referrals", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddConstraint(
            model_name="ledger",
            constraint=models.UniqueConstraint(condition=Q(kind="referral"), fields=("kind", "ref"), name="one_referral_per_order"),
        ),
        migrations.RunPython(rename_codes, migrations.RunPython.noop),
    ]
