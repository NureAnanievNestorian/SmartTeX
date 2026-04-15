from django.contrib import admin

from .models import MCPToken


@admin.register(MCPToken)
class MCPTokenAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "created_at")
    search_fields = ("user__username", "user__email")
