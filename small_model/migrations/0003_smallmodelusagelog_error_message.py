from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("small_model", "0002_smallmodelusagelog_prompts"),
    ]

    operations = [
        migrations.AddField(
            model_name="smallmodelusagelog",
            name="error_message",
            field=models.TextField(blank=True),
        ),
    ]
