from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("small_model", "0003_smallmodelusagelog_error_message"),
    ]

    operations = [
        migrations.AlterField(
            model_name="usersmallmodelaccess",
            name="provider",
            field=models.CharField(
                choices=[
                    ("gemini", "Gemini"),
                    ("deepseek", "DeepSeek"),
                    ("mock", "Mock"),
                    ("openai", "OpenAI"),
                ],
                default="gemini",
                max_length=30,
            ),
        ),
    ]
