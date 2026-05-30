from django.conf import settings
from django.db import models

from SmartTeX.markup import MarkupType
from templates_lib.models import Template


class Project(models.Model):
    class CompileStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        ERROR = "error", "Error"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="projects")
    title = models.CharField(max_length=255)
    template = models.ForeignKey(Template, null=True, blank=True, on_delete=models.SET_NULL)
    markup_type = models.CharField(max_length=10, choices=MarkupType.choices, default=MarkupType.LATEX)
    main_file = models.CharField(max_length=255, blank=True, default="")
    last_status = models.CharField(max_length=16, choices=CompileStatus.choices, default=CompileStatus.PENDING)
    github_sync_enabled = models.BooleanField(default=False)
    github_repo_url = models.CharField(max_length=512, blank=True, default="")
    github_pat = models.CharField(max_length=256, blank=True, default="")
    github_sync_interval_minutes = models.PositiveIntegerField(default=30)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"{self.title} ({self.owner_id})"


class ProjectVersion(models.Model):
    class SnapshotKind(models.TextChoices):
        TEXT = "text", "Text"
        EVENT = "event", "Event"

    class Category(models.TextChoices):
        SOURCE = "source", "Source"
        ASSISTANT = "assistant", "Assistant"
        SESSION_ACCEPT = "session_accept", "Session Accept"

    class Source(models.TextChoices):
        MCP = "mcp", "MCP"
        WEB = "web", "Web"
        API = "api", "API"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="versions")
    number = models.PositiveIntegerField(default=1)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.API)
    operation = models.CharField(max_length=64)
    target = models.CharField(max_length=255, default="main.tex")
    target_file = models.CharField(max_length=255, default="", blank=True)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.SOURCE)
    snapshot_kind = models.CharField(max_length=12, choices=SnapshotKind.choices, default=SnapshotKind.TEXT)
    event_payload = models.JSONField(default=dict, blank=True)
    is_revertible = models.BooleanField(default=True)
    summary = models.TextField()
    before_content = models.TextField()
    after_content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        unique_together = [("project", "number")]

    def __str__(self) -> str:
        return f"v{self.number} {self.project_id} {self.operation}"
