from django.contrib import admin

from .models import Template


@admin.register(Template)
class TemplateAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "category", "markup_type", "has_zip", "is_active", "updated_at")
    list_filter = ("category", "markup_type", "is_active")
    search_fields = ("title", "description")
    fieldsets = (
        (None, {"fields": ("title", "description", "category", "markup_type", "is_active")}),
        ("Content", {"fields": ("content",)}),
        ("Multifile ZIP (optional)", {
            "description": "Upload a .zip archive to seed new projects with multiple files. "
                           "Files in the ZIP will be extracted into the project alongside the main file. "
                           "If the ZIP contains main.tex or main.typ it will override the Content field above.",
            "fields": ("zip_file",),
        }),
    )

    @admin.display(boolean=True, description="ZIP")
    def has_zip(self, obj):
        return bool(obj.zip_file)
