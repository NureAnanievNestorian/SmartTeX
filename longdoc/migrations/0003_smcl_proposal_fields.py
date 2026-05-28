from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("longdoc", "0002_changeproposal"),
    ]

    operations = [
        migrations.AddField(
            model_name="changeproposal",
            name="smcl_risk_level",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="changeproposal",
            name="smcl_warnings",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="changeproposal",
            name="smcl_metadata",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
