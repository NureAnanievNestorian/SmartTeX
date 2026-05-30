from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("longdoc", "0004_preparation_enforcement_mode"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="projectlongdocsettings",
            name="preparation_enforcement_mode",
        ),
    ]
