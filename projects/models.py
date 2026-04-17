from django.conf import settings
from django.db import models

from templates_lib.models import Template


class Project(models.Model):
    class CompileStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        ERROR = "error", "Error"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="projects")
    title = models.CharField(max_length=255)
    template = models.ForeignKey(Template, null=True, blank=True, on_delete=models.SET_NULL)
    last_status = models.CharField(max_length=16, choices=CompileStatus.choices, default=CompileStatus.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"{self.title} ({self.owner_id})"


class ProjectVersion(models.Model):
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
    summary = models.CharField(max_length=255)
    before_content = models.TextField()
    after_content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        unique_together = [("project", "number")]

    def __str__(self) -> str:
        return f"v{self.number} {self.project_id} {self.operation}"
