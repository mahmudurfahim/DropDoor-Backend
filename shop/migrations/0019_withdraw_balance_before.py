from django.db import migrations, models


def fill(apps, schema_editor):
    """Old requests: the balance just before the withdraw's own ledger row (exact, the ledger is append-only)."""
    Ledger = apps.get_model("shop", "Ledger")
    W = apps.get_model("shop", "Withdraw")
    rows = []
    for w in W.objects.filter(balance_before__isnull=True).iterator():
        lr = Ledger.objects.filter(user_id=w.user_id, kind="withdraw", ref=str(w.id)).first()
        if lr:
            w.balance_before = sum(Ledger.objects.filter(user_id=w.user_id, id__lt=lr.id).values_list("amount", flat=True))
            rows.append(w)
    if rows:
        W.objects.bulk_update(rows, ["balance_before"])


class Migration(migrations.Migration):

    dependencies = [
        ("shop", "0018_delete_adminalert"),
    ]

    operations = [
        migrations.AddField(model_name="withdraw", name="balance_before", field=models.IntegerField(blank=True, null=True)),
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
