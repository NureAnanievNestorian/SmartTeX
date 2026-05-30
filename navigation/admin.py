from django.contrib import admin

from .models import FileCard, ProjectNavigationIndex, RegionCard


class RegionCardInline(admin.TabularInline):
    model = RegionCard
    extra = 0
    fields = (
        "order",
        "region_kind",
        "title",
        "level",
        "line_start",
        "line_end",
        "state",
        "state_confidence",
        "is_stale",
    )
    readonly_fields = fields
    can_delete = False
    show_change_link = True


@admin.register(ProjectNavigationIndex)
class ProjectNavigationIndexAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "project",
        "status",
        "schema_version",
        "entrypoint_file",
        "last_built_version_number",
        "last_built_at",
        "last_partial_refresh_at",
        "updated_at",
    )
    list_filter = ("status", "schema_version", "markup_type_snapshot")
    search_fields = ("project__title", "entrypoint_file", "main_file_snapshot", "build_error")
    readonly_fields = (
        "created_at",
        "updated_at",
        "last_built_at",
        "last_partial_refresh_at",
    )


@admin.register(FileCard)
class FileCardAdmin(admin.ModelAdmin):
    list_display = (
        "filename",
        "project_id",
        "role",
        "state",
        "reachability",
        "line_count",
        "is_stale",
        "last_version_number",
        "updated_at",
    )
    list_filter = ("role", "state", "reachability", "is_stale", "role_source", "state_source")
    search_fields = ("filename", "summary", "index__project__title")
    readonly_fields = ("created_at", "updated_at", "last_indexed_at")
    inlines = (RegionCardInline,)

    @admin.display(description="Project id")
    def project_id(self, obj):
        return obj.index.project_id


@admin.register(RegionCard)
class RegionCardAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "filename",
        "region_kind",
        "line_start",
        "line_end",
        "state",
        "is_stale",
        "updated_at",
    )
    list_filter = ("region_kind", "state", "is_stale", "state_source")
    search_fields = ("title", "summary", "file_card__filename", "file_card__index__project__title")
    readonly_fields = ("created_at", "updated_at", "last_indexed_at")

    @admin.display(description="Filename")
    def filename(self, obj):
        return obj.file_card.filename
