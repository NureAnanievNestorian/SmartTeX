from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("small_model", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="smallmodelusagelog",
            name="input_prompt",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="smallmodelusagelog",
            name="output_text",
            field=models.TextField(blank=True),
        ),
    ]
