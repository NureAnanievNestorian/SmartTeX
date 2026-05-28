from django.contrib import admin

from .models import (
    AIBatch,
    AIBatchChange,
    AISession,
    AssistantAuditLog,
    ProjectContextFile,
    ProjectLongDocSettings,
    ProjectNoteSection,
    ProjectOutlineItem,
    ProjectRequirement,
    ProjectTask,
    RequirementSectionRef,
    SectionSummary,
)


@admin.register(ProjectLongDocSettings)
class ProjectLongDocSettingsAdmin(admin.ModelAdmin):
    list_display = ("project", "enabled", "ai_sessions_enabled", "mcp_controlled_access", "updated_at")
    list_filter = ("enabled", "ai_sessions_enabled", "mcp_controlled_access")
    search_fields = ("project__title", "project__owner__username", "project__owner__email")


@admin.register(ProjectContextFile)
class ProjectContextFileAdmin(admin.ModelAdmin):
    list_display = ("project", "filename", "is_read_only", "size_bytes", "updated_at")
    list_filter = ("is_read_only",)
    search_fields = ("project__title", "filename", "display_name")


@admin.register(ProjectOutlineItem)
class ProjectOutlineItemAdmin(admin.ModelAdmin):
    list_display = ("project", "order", "title", "level", "status", "updated_at")
    list_filter = ("status", "level")
    search_fields = ("project__title", "title")


@admin.register(ProjectTask)
class ProjectTaskAdmin(admin.ModelAdmin):
    list_display = ("project", "status", "created_by", "completed_at", "updated_at")
    list_filter = ("status", "created_by")
    search_fields = ("project__title", "description")


@admin.register(ProjectNoteSection)
class ProjectNoteSectionAdmin(admin.ModelAdmin):
    list_display = ("project", "order", "heading", "updated_at")
    search_fields = ("project__title", "heading", "body")


@admin.register(SectionSummary)
class SectionSummaryAdmin(admin.ModelAdmin):
    list_display = ("project", "section_title", "source_file", "written_by", "is_stale", "updated_at")
    list_filter = ("written_by", "is_stale")
    search_fields = ("project__title", "section_title", "source_file")


class RequirementSectionRefInline(admin.TabularInline):
    model = RequirementSectionRef
    extra = 0


@admin.register(ProjectRequirement)
class ProjectRequirementAdmin(admin.ModelAdmin):
    list_display = ("project", "req_id", "coverage", "updated_by", "updated_at")
    list_filter = ("coverage", "updated_by")
    search_fields = ("project__title", "req_id", "description")
    inlines = [RequirementSectionRefInline]


class AIBatchChangeInline(admin.TabularInline):
    model = AIBatchChange
    extra = 0


@admin.register(AISession)
class AISessionAdmin(admin.ModelAdmin):
    list_display = ("project", "status", "compile_status", "created_by_scope", "expires_at", "updated_at")
    list_filter = ("status", "compile_status", "created_by_scope")
    search_fields = ("project__title", "branch_name", "goal")


@admin.register(AIBatch)
class AIBatchAdmin(admin.ModelAdmin):
    list_display = ("session", "notes_updated", "requirements_updated", "updated_at")
    search_fields = ("session__project__title", "summary")
    inlines = [AIBatchChangeInline]


@admin.register(AssistantAuditLog)
class AssistantAuditLogAdmin(admin.ModelAdmin):
    list_display = ("project", "model_name", "operation", "source", "actor", "created_at")
    list_filter = ("operation", "source", "model_name")
    search_fields = ("project__title", "model_name", "summary", "actor__username", "actor__email")
