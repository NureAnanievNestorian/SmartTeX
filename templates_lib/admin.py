from django.contrib import admin

from .models import Template


@admin.register(Template)
class TemplateAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "category", "markup_type", "main_file", "has_zip", "is_active", "updated_at")
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

    @admin.display(boolean=True, description="ZIP")
    def has_zip(self, obj):
        return bool(obj.zip_file)
