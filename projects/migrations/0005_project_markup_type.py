from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0004_projectversion_unique_number"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="markup_type",
            field=models.CharField(
                choices=[("latex", "LaTeX"), ("typst", "Typst")],
                default="latex",
                max_length=10,
            ),
        ),
    ]
