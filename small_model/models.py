from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from projects.models import Project

from .task_types import FEATURE_KEYS, TASK_TYPES


def next_utc_midnight():
    now = timezone.now()
    tomorrow = (now + timedelta(days=1)).date()
    return timezone.datetime.combine(tomorrow, timezone.datetime.min.time(), tzinfo=timezone.UTC)


def next_month_utc():
    now = timezone.now()
    year = now.year + (1 if now.month == 12 else 0)
    month = 1 if now.month == 12 else now.month + 1
    return timezone.datetime(year, month, 1, tzinfo=timezone.UTC)


class UserSmallModelAccess(models.Model):
    class Provider(models.TextChoices):
        GEMINI = "gemini", "Gemini"
        MOCK = "mock", "Mock"
        OPENAI = "openai", "OpenAI"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="small_model_access")
    enabled = models.BooleanField(default=False)
    provider = models.CharField(max_length=30, choices=Provider.choices, default=Provider.GEMINI)
    model_name = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def has_feature(self, feature_key: str) -> bool:
        if not self.enabled:
            return False
        return self.feature_grants.filter(feature_key=feature_key).exists()

    def __str__(self) -> str:
        return f"{self.user_id}:{self.provider}:{'enabled' if self.enabled else 'disabled'}"


class UserSmallModelFeatureGrant(models.Model):
    access = models.ForeignKey(UserSmallModelAccess, on_delete=models.CASCADE, related_name="feature_grants")
    feature_key = models.CharField(max_length=50, choices=[(key, key) for key in FEATURE_KEYS])
    granted_at = models.DateTimeField(auto_now_add=True)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="small_model_feature_grants_made",
    )

    class Meta:
        unique_together = [("access", "feature_key")]

    def __str__(self) -> str:
        return f"{self.access_id}:{self.feature_key}"


class UserSmallModelQuota(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="small_model_quota")
    daily_request_limit = models.PositiveIntegerField(default=50)
    monthly_request_limit = models.PositiveIntegerField(default=500)
    daily_token_limit = models.PositiveIntegerField(default=100_000)
    monthly_token_limit = models.PositiveIntegerField(default=1_000_000)
    daily_requests_used = models.PositiveIntegerField(default=0)
    monthly_requests_used = models.PositiveIntegerField(default=0)
    daily_tokens_used = models.PositiveIntegerField(default=0)
    monthly_tokens_used = models.PositiveIntegerField(default=0)
    daily_reset_at = models.DateTimeField(default=next_utc_midnight)
    monthly_reset_at = models.DateTimeField(default=next_month_utc)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.user_id}: {self.daily_requests_used}/{self.daily_request_limit} today"


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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def feature_enabled(self, feature_key: str) -> bool:
        field = {
            "context_compressor": "context_compressor_enabled",
            "edit_intent_classifier": "edit_intent_classifier_enabled",
            "diff_safety_reviewer": "diff_safety_reviewer_enabled",
            "compile_log_triage": "compile_log_triage_enabled",
            "circuit_breaker": "circuit_breaker_enabled",
        }.get(feature_key)
        return bool(self.small_model_control_enabled and field and getattr(self, field, False))

    def __str__(self) -> str:
        return f"SMCL settings for project {self.project_id}"
