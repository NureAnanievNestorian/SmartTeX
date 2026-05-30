from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("longdoc", "0003_smcl_proposal_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="projectlongdocsettings",
            name="preparation_enforcement_mode",
            field=models.CharField(
                max_length=20,
                default="off",
                choices=[
                    ("off", "Off"),
                    ("warn", "Warn"),
                    ("block_broad_reads", "Block broad reads"),
                ],
            ),
        ),
    ]
