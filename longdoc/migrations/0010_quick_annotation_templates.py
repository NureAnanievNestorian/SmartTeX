from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("longdoc", "0009_changeproposaldiffannotation"),
    ]

    operations = [
        migrations.AddField(
            model_name="projectlongdocsettings",
            name="quick_annotation_templates",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
