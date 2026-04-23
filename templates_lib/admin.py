from django.contrib import admin

from .models import Template


@admin.register(Template)
class TemplateAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "category", "markup_type", "is_active", "updated_at")
    list_filter = ("category", "markup_type", "is_active")
    search_fields = ("title", "description")
