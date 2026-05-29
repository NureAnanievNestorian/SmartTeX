from decimal import Decimal

from django.contrib import admin

from .models import (
    ProjectSmallModelSettings,
    SmallModelConfig,
    SmallModelUsageLog,
    UserSmallModelAccess,
    UserSmallModelQuota,
)


@admin.register(UserSmallModelAccess)
class UserSmallModelAccessAdmin(admin.ModelAdmin):
    list_display = ("user", "enabled", "model_config", "updated_at")
    list_filter = ("enabled", "model_config__provider")
    search_fields = ("user__username", "user__email", "notes")


@admin.action(description="Reset credits to zero")
def reset_credits(modeladmin, request, queryset):
    queryset.update(credits_used=Decimal("0"))


@admin.register(UserSmallModelQuota)
class UserSmallModelQuotaAdmin(admin.ModelAdmin):
    list_display = ("user", "credits_used", "credits_limit", "updated_at")
    search_fields = ("user__username", "user__email")
    actions = [reset_credits]


@admin.register(SmallModelConfig)
class SmallModelConfigAdmin(admin.ModelAdmin):
    list_display = ("provider", "model_name", "input_price_per_million_tokens", "output_price_per_million_tokens", "is_active", "updated_at")
    list_filter = ("provider", "is_active")
    search_fields = ("provider", "model_name")


@admin.register(SmallModelUsageLog)
class SmallModelUsageLogAdmin(admin.ModelAdmin):
    list_display = ("user", "project", "provider", "model_name", "task_type", "status", "latency_ms", "created_at")
    list_filter = ("provider", "task_type", "status", "created_at")
    search_fields = ("user__username", "user__email", "project__title", "error_code")
    readonly_fields = [field.name for field in SmallModelUsageLog._meta.fields]
    fieldsets = (
        (None, {"fields": ("user", "project", "provider", "model_name", "task_type", "status", "error_code", "error_message", "created_at")}),
        ("Tokens & Latency", {"fields": ("input_tokens_estimate", "output_tokens_estimate", "latency_ms")}),
        ("Prompts (logged only when SMALL_MODEL_LOG_PROMPTS=True)", {
            "classes": ("collapse",),
            "fields": ("input_prompt", "output_text"),
        }),
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ProjectSmallModelSettings)
class ProjectSmallModelSettingsAdmin(admin.ModelAdmin):
    list_display = ("project", "small_model_control_enabled", "diff_safety_reviewer_enabled", "updated_at")
    list_filter = ("small_model_control_enabled", "diff_safety_reviewer_enabled")
    search_fields = ("project__title", "project__owner__username", "project__owner__email")
