from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("small_model", "0008_drop_feature_grants"),
    ]

    operations = [
        migrations.AddField(
            model_name="projectsmallmodelsettings",
            name="nav_index_enrich_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="projectsmallmodelsettings",
            name="nav_rerank_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="projectsmallmodelsettings",
            name="nav_repair_enabled",
            field=models.BooleanField(default=False),
        ),
    ]
