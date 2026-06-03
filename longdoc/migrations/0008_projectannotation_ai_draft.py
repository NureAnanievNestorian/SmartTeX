from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("longdoc", "0007_aibatch_annotations_completed"),
    ]

    operations = [
        migrations.AlterField(
            model_name="projectannotation",
            name="status",
            field=models.CharField(
                choices=[
                    ("ai_draft", "AI Draft"),
                    ("open", "Open"),
                    ("in_progress", "In Progress"),
                    ("done", "Done"),
                    ("dismissed", "Dismissed"),
                ],
                default="open",
                max_length=20,
            ),
        ),
    ]
