from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("navigation", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="projectnavigationindex",
            name="schema_version",
            field=models.PositiveSmallIntegerField(default=2),
        ),
    ]
