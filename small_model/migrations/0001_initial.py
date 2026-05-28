import django.db.models.deletion
import small_model.models
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("projects", "0009_projectversion_category"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserSmallModelAccess",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("enabled", models.BooleanField(default=False)),
                ("provider", models.CharField(choices=[("gemini", "Gemini"), ("mock", "Mock"), ("openai", "OpenAI")], default="gemini", max_length=30)),
                ("model_name", models.CharField(blank=True, max_length=100)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="small_model_access", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="UserSmallModelQuota",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("daily_request_limit", models.PositiveIntegerField(default=50)),
                ("monthly_request_limit", models.PositiveIntegerField(default=500)),
                ("daily_token_limit", models.PositiveIntegerField(default=100000)),
                ("monthly_token_limit", models.PositiveIntegerField(default=1000000)),
                ("daily_requests_used", models.PositiveIntegerField(default=0)),
                ("monthly_requests_used", models.PositiveIntegerField(default=0)),
                ("daily_tokens_used", models.PositiveIntegerField(default=0)),
                ("monthly_tokens_used", models.PositiveIntegerField(default=0)),
                ("daily_reset_at", models.DateTimeField(default=small_model.models.next_utc_midnight)),
                ("monthly_reset_at", models.DateTimeField(default=small_model.models.next_month_utc)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="small_model_quota", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="ProjectSmallModelSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("small_model_control_enabled", models.BooleanField(default=False)),
                ("context_compressor_enabled", models.BooleanField(default=False)),
                ("edit_intent_classifier_enabled", models.BooleanField(default=False)),
                ("diff_safety_reviewer_enabled", models.BooleanField(default=False)),
                ("compile_log_triage_enabled", models.BooleanField(default=False)),
                ("circuit_breaker_enabled", models.BooleanField(default=False)),
                ("minimal_patch_generator_enabled", models.BooleanField(default=False)),
                ("post_edit_success_judge_enabled", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("project", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="small_model_settings", to="projects.project")),
            ],
        ),
        migrations.CreateModel(
            name="SmallModelUsageLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("provider", models.CharField(max_length=50)),
                ("model_name", models.CharField(blank=True, max_length=100)),
                ("task_type", models.CharField(choices=[("context_compress", "context_compress"), ("edit_intent_classify", "edit_intent_classify"), ("diff_safety_review", "diff_safety_review"), ("compile_log_triage", "compile_log_triage"), ("circuit_breaker_evaluate", "circuit_breaker_evaluate")], max_length=60)),
                ("status", models.CharField(choices=[("success", "Success"), ("quota_exceeded", "Quota Exceeded"), ("timeout", "Timeout"), ("invalid_json", "Invalid JSON"), ("provider_error", "Provider Error")], max_length=30)),
                ("input_tokens_estimate", models.PositiveIntegerField(default=0)),
                ("output_tokens_estimate", models.PositiveIntegerField(default=0)),
                ("latency_ms", models.PositiveIntegerField(default=0)),
                ("error_code", models.CharField(blank=True, max_length=80)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("project", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="small_model_usage_logs", to="projects.project")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="small_model_usage_logs", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="UserSmallModelFeatureGrant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("feature_key", models.CharField(choices=[("context_compressor", "context_compressor"), ("edit_intent_classifier", "edit_intent_classifier"), ("diff_safety_reviewer", "diff_safety_reviewer"), ("compile_log_triage", "compile_log_triage"), ("circuit_breaker", "circuit_breaker")], max_length=50)),
                ("granted_at", models.DateTimeField(auto_now_add=True)),
                ("access", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="feature_grants", to="small_model.usersmallmodelaccess")),
                ("granted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="small_model_feature_grants_made", to=settings.AUTH_USER_MODEL)),
            ],
            options={"unique_together": {("access", "feature_key")}},
        ),
        migrations.AddIndex(model_name="smallmodelusagelog", index=models.Index(fields=["user", "-created_at"], name="small_model_user_id_52892d_idx")),
        migrations.AddIndex(model_name="smallmodelusagelog", index=models.Index(fields=["project", "-created_at"], name="small_model_project_a13ddf_idx")),
        migrations.AddIndex(model_name="smallmodelusagelog", index=models.Index(fields=["task_type", "status"], name="small_model_task_ty_19bf06_idx")),
    ]
