from django.db import migrations


class Migration(migrations.Migration):
    """Only the names shown in Django admin change (Users -> Resellers). No table or column is touched."""
    dependencies = [("shop", "0025_page_link_profile_edit")]

    operations = [
        migrations.AlterModelOptions(name="user", options={"verbose_name": "reseller", "verbose_name_plural": "resellers"}),
    ]
