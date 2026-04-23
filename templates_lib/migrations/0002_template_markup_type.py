from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("templates_lib", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="template",
            name="markup_type",
            field=models.CharField(
                choices=[("latex", "LaTeX"), ("typst", "Typst")],
                default="latex",
                max_length=10,
            ),
        ),
    ]
