from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("small_model", "0007_remove_monthly_reset_at"),
    ]

    operations = [
        migrations.DeleteModel(name="UserSmallModelFeatureGrant"),
    ]
