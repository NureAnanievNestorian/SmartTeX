from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from projects.models import Project

from .task_types import TASK_TYPES


def next_utc_midnight():
    now = timezone.now()
    tomorrow = (now + timedelta(days=1)).date()
    return timezone.datetime.combine(tomorrow, timezone.datetime.min.time(), tzinfo=timezone.UTC)


def next_month_utc():
    now = timezone.now()
    year = now.year + (1 if now.month == 12 else 0)
    month = 1 if now.month == 12 else now.month + 1
    return timezone.datetime(year, month, 1, tzinfo=timezone.UTC)


class SmallModelConfig(models.Model):
    """Central registry of provider+model pairs: pricing and runtime config."""

    class Provider(models.TextChoices):
        GEMINI = "gemini", "Gemini"
        DEEPSEEK = "deepseek", "DeepSeek"
        OPENAI = "openai", "OpenAI"
        MOCK = "mock", "Mock"

    provider = models.CharField(max_length=30, choices=Provider.choices)
    model_name = models.CharField(max_length=100)
    input_price_per_million_tokens = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"))
    output_price_per_million_tokens = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"))
    provider_config = models.JSONField(
        default=dict,
        blank=True,
        help_text="Runtime config: timeout_seconds, max_output_tokens, temperature, top_p, etc.",
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("provider", "model_name")]

    def __str__(self) -> str:
        return f"{self.provider}/{self.model_name}"


class UserSmallModelAccess(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="small_model_access")
    enabled = models.BooleanField(default=False)
    model_config = models.ForeignKey(
        SmallModelConfig,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="access_records",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def has_feature(self, feature_key: str) -> bool:
        return self.enabled

    def __str__(self) -> str:
        label = str(self.model_config) if self.model_config_id else "no config"
        return f"{self.user_id}:{label}:{'enabled' if self.enabled else 'disabled'}"


class UserSmallModelQuota(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="small_model_quota")
    credits_limit = models.DecimalField(max_digits=10, decimal_places=6, default=Decimal("1.000000"))
    credits_used = models.DecimalField(max_digits=10, decimal_places=6, default=Decimal("0"))
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.user_id}: ${self.credits_used}/{self.credits_limit}"


class SmallModelUsageLog(models.Model):
    class Status(models.TextChoices):
        SUCCESS = "success", "Success"
        QUOTA_EXCEEDED = "quota_exceeded", "Quota Exceeded"
        TIMEOUT = "timeout", "Timeout"
        INVALID_JSON = "invalid_json", "Invalid JSON"
        PROVIDER_ERROR = "provider_error", "Provider Error"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="small_model_usage_logs")
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="small_model_usage_logs")
    provider = models.CharField(max_length=50)
    model_name = models.CharField(max_length=100, blank=True)
    task_type = models.CharField(max_length=60, choices=[(key, key) for key in TASK_TYPES])
    status = models.CharField(max_length=30, choices=Status.choices)
    input_tokens_estimate = models.PositiveIntegerField(default=0)
    output_tokens_estimate = models.PositiveIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=80, blank=True)
    error_message = models.TextField(blank=True)
    input_prompt = models.TextField(blank=True)
    output_text = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["project", "-created_at"]),
            models.Index(fields=["task_type", "status"]),
        ]


class ProjectSmallModelSettings(models.Model):
    project = models.OneToOneField(Project, on_delete=models.CASCADE, related_name="small_model_settings")
    small_model_control_enabled = models.BooleanField(default=False)
    context_compressor_enabled = models.BooleanField(default=False)
    edit_intent_classifier_enabled = models.BooleanField(default=False)
    diff_safety_reviewer_enabled = models.BooleanField(default=False)
    compile_log_triage_enabled = models.BooleanField(default=False)
    circuit_breaker_enabled = models.BooleanField(default=False)
    minimal_patch_generator_enabled = models.BooleanField(default=False)
    post_edit_success_judge_enabled = models.BooleanField(default=False)
    nav_index_enrich_enabled = models.BooleanField(default=False)
    nav_rerank_enabled = models.BooleanField(default=False)
    nav_repair_enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def feature_enabled(self, feature_key: str) -> bool:
        field = {
            "context_compressor": "context_compressor_enabled",
            "edit_intent_classifier": "edit_intent_classifier_enabled",
            "diff_safety_reviewer": "diff_safety_reviewer_enabled",
            "compile_log_triage": "compile_log_triage_enabled",
            "circuit_breaker": "circuit_breaker_enabled",
            "nav_index_enrich": "nav_index_enrich_enabled",
            "nav_rerank": "nav_rerank_enabled",
            "nav_repair": "nav_repair_enabled",
        }.get(feature_key)
        if not field:
            return False
        # Navigation features are project-assistant capabilities, not part of
        # the old safety-layer master switch. User/account access and quota are
        # still enforced by SmallModelCallMixin before a provider call.
        if feature_key in {"nav_index_enrich", "nav_rerank", "nav_repair"}:
            return bool(getattr(self, field, False))
        return bool(self.small_model_control_enabled and getattr(self, field, False))

    def __str__(self) -> str:
        return f"SMCL settings for project {self.project_id}"
