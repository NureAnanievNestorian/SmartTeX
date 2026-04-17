from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0003_projectversion_number"),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name="projectversion",
            unique_together={("project", "number")},
        ),
    ]
