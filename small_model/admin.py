from django.contrib import admin

from .models import (
    ProjectSmallModelSettings,
    SmallModelUsageLog,
    UserSmallModelAccess,
    UserSmallModelFeatureGrant,
    UserSmallModelQuota,
    next_month_utc,
    next_utc_midnight,
)


class UserSmallModelFeatureGrantInline(admin.TabularInline):
    model = UserSmallModelFeatureGrant
    extra = 0


@admin.register(UserSmallModelAccess)
class UserSmallModelAccessAdmin(admin.ModelAdmin):
    list_display = ("user", "enabled", "provider", "model_name", "updated_at")
    list_filter = ("enabled", "provider")
    search_fields = ("user__username", "user__email", "notes")
    inlines = [UserSmallModelFeatureGrantInline]


@admin.action(description="Reset daily quota")
def reset_daily_quota(modeladmin, request, queryset):
    queryset.update(daily_requests_used=0, daily_tokens_used=0, daily_reset_at=next_utc_midnight())


@admin.action(description="Reset monthly quota")
def reset_monthly_quota(modeladmin, request, queryset):
    queryset.update(monthly_requests_used=0, monthly_tokens_used=0, monthly_reset_at=next_month_utc())


@admin.register(UserSmallModelQuota)
class UserSmallModelQuotaAdmin(admin.ModelAdmin):
    list_display = ("user", "daily_requests_used", "daily_request_limit", "daily_tokens_used", "daily_token_limit")
    search_fields = ("user__username", "user__email")
    actions = [reset_daily_quota, reset_monthly_quota]


@admin.register(SmallModelUsageLog)
class SmallModelUsageLogAdmin(admin.ModelAdmin):
    list_display = ("user", "project", "provider", "model_name", "task_type", "status", "created_at")
    list_filter = ("provider", "task_type", "status", "created_at")
    search_fields = ("user__username", "user__email", "project__title", "error_code")
    readonly_fields = [field.name for field in SmallModelUsageLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ProjectSmallModelSettings)
class ProjectSmallModelSettingsAdmin(admin.ModelAdmin):
    list_display = ("project", "small_model_control_enabled", "diff_safety_reviewer_enabled", "updated_at")
    list_filter = ("small_model_control_enabled", "diff_safety_reviewer_enabled")
    search_fields = ("project__title", "project__owner__username", "project__owner__email")
