from django.contrib import admin

from .models import Project


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "owner", "template", "last_status", "updated_at")
    list_filter = ("last_status", "template")
    search_fields = ("title", "owner__username", "owner__email")
