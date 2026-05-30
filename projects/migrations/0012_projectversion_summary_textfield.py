from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0011_project_github_sync_interval"),
    ]

    operations = [
        migrations.AlterField(
            model_name="projectversion",
            name="summary",
            field=models.TextField(),
        ),
    ]
