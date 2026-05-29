from decimal import Decimal

from django.db import migrations, models
import small_model.models


class Migration(migrations.Migration):

    dependencies = [
        ("small_model", "0004_add_deepseek_provider"),
    ]

    operations = [
        migrations.CreateModel(
            name="SmallModelPricing",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("provider", models.CharField(max_length=30)),
                ("model_name", models.CharField(max_length=100)),
                ("input_price_per_million_tokens", models.DecimalField(decimal_places=6, default=Decimal("0"), max_digits=12)),
                ("output_price_per_million_tokens", models.DecimalField(decimal_places=6, default=Decimal("0"), max_digits=12)),
                ("is_active", models.BooleanField(default=True)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.AlterUniqueTogether(
            name="smallmodelpricing",
            unique_together={("provider", "model_name")},
        ),
        migrations.RemoveField(model_name="usersmallmodelquota", name="daily_request_limit"),
        migrations.RemoveField(model_name="usersmallmodelquota", name="monthly_request_limit"),
        migrations.RemoveField(model_name="usersmallmodelquota", name="daily_token_limit"),
        migrations.RemoveField(model_name="usersmallmodelquota", name="monthly_token_limit"),
        migrations.RemoveField(model_name="usersmallmodelquota", name="daily_requests_used"),
        migrations.RemoveField(model_name="usersmallmodelquota", name="monthly_requests_used"),
        migrations.RemoveField(model_name="usersmallmodelquota", name="daily_tokens_used"),
        migrations.RemoveField(model_name="usersmallmodelquota", name="monthly_tokens_used"),
        migrations.RemoveField(model_name="usersmallmodelquota", name="daily_reset_at"),
        migrations.AddField(
            model_name="usersmallmodelquota",
            name="credits_limit",
            field=models.DecimalField(decimal_places=6, default=Decimal("1.000000"), max_digits=10),
        ),
        migrations.AddField(
            model_name="usersmallmodelquota",
            name="credits_used",
            field=models.DecimalField(decimal_places=6, default=Decimal("0"), max_digits=10),
        ),
    ]
