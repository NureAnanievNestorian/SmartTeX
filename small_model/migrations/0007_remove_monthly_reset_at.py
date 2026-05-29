from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("small_model", "0006_access_pricing_fk"),
    ]

    operations = [
        migrations.RemoveField(model_name="usersmallmodelquota", name="monthly_reset_at"),
    ]
