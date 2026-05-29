import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("small_model", "0005_credits_rework"),
    ]

    operations = [
        # Rename SmallModelPricing → SmallModelConfig
        migrations.RenameModel(
            old_name="SmallModelPricing",
            new_name="SmallModelConfig",
        ),
        # Add choices to provider field and provider_config JSON field
        migrations.AlterField(
            model_name="smallmodelconfig",
            name="provider",
            field=models.CharField(
                choices=[
                    ("gemini", "Gemini"),
                    ("deepseek", "DeepSeek"),
                    ("openai", "OpenAI"),
                    ("mock", "Mock"),
                ],
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="smallmodelconfig",
            name="provider_config",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Runtime config: timeout_seconds, max_output_tokens, temperature, top_p, etc.",
            ),
        ),
        # Replace plain provider/model_name strings on access with FK to SmallModelConfig
        migrations.AddField(
            model_name="usersmallmodelaccess",
            name="model_config",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="access_records",
                to="small_model.smallmodelconfig",
            ),
        ),
        migrations.RemoveField(model_name="usersmallmodelaccess", name="provider"),
        migrations.RemoveField(model_name="usersmallmodelaccess", name="model_name"),
    ]
