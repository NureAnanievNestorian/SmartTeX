from django.contrib import admin

from .models import (
    Template,
    TemplateContextFile,
    TemplateLongDocDefaults,
    TemplateNoteSection,
    TemplateOutlineItem,
    TemplateRequirement,
    TemplateTask,
)


class TemplateLongDocDefaultsInline(admin.StackedInline):
    model = TemplateLongDocDefaults
    extra = 0
    max_num = 1
    can_delete = True
    verbose_name = "Long-document defaults"
    verbose_name_plural = "Long-document defaults"


class TemplateOutlineItemInline(admin.TabularInline):
    model = TemplateOutlineItem
    extra = 0
    fields = ("order", "title", "level", "status", "expected_pages", "notes")


class TemplateRequirementInline(admin.TabularInline):
    model = TemplateRequirement
    extra = 0
    fields = ("req_id", "description")


class TemplateTaskInline(admin.TabularInline):
    model = TemplateTask
    extra = 0
    fields = ("description",)


class TemplateNoteSectionInline(admin.TabularInline):
    model = TemplateNoteSection
    extra = 0
    fields = ("order", "heading", "body")


class TemplateContextFileInline(admin.TabularInline):
    model = TemplateContextFile
    extra = 0
    fields = ("filename", "display_name", "description", "content")
    show_change_link = True


@admin.register(Template)
class TemplateAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "category", "markup_type", "main_file", "has_zip", "has_longdoc", "is_active", "updated_at")
    list_filter = ("category", "markup_type", "is_active")
    search_fields = ("title", "description")
    fieldsets = (
        (None, {"fields": ("title", "description", "category", "markup_type", "is_active")}),
        ("Content", {"fields": ("content",)}),
        ("Multifile ZIP (optional)", {
            "description": "Upload a .zip archive to seed new projects with multiple files. "
                           "Files in the ZIP will be extracted into the project alongside the main file. "
                           "Set Main file when the archive entry point is not main.tex/main.typ, for example report/main.tex or thesis.typ.",
            "fields": ("zip_file", "main_file"),
        }),
    )
    inlines = [
        TemplateLongDocDefaultsInline,
        TemplateOutlineItemInline,
        TemplateRequirementInline,
        TemplateTaskInline,
        TemplateNoteSectionInline,
        TemplateContextFileInline,
    ]

    @admin.display(boolean=True, description="ZIP")
    def has_zip(self, obj):
        return bool(obj.zip_file)

    @admin.display(boolean=True, description="LongDoc")
    def has_longdoc(self, obj):
        return hasattr(obj, "longdoc_defaults")
