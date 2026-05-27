from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0005_project_markup_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="projectversion",
            name="event_payload",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="projectversion",
            name="is_revertible",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="projectversion",
            name="snapshot_kind",
            field=models.CharField(
                choices=[("text", "Text"), ("event", "Event")],
                default="text",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="projectversion",
            name="target_file",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
