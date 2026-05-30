from django.conf import settings
from django.db import models
from django.db.models import Q

from projects.models import Project


LOCKING_AI_SESSION_STATUSES = ("active", "compiled", "ready_for_review")
LOCKING_CHANGE_PROPOSAL_STATUSES = (
    "draft",
    "validating",
    "failed_validation",
    "failed_compile",
    "ready_for_review",
)


class ProjectLongDocSettings(models.Model):
    project = models.OneToOneField(Project, on_delete=models.CASCADE, related_name="longdoc_settings")
    enabled = models.BooleanField(default=False)
    context_enabled = models.BooleanField(default=True)
    outline_enabled = models.BooleanField(default=True)
    tasks_enabled = models.BooleanField(default=True)
    notes_enabled = models.BooleanField(default=True)
    summaries_enabled = models.BooleanField(default=True)
    requirements_enabled = models.BooleanField(default=False)
    ai_sessions_enabled = models.BooleanField(default=True)
    mcp_controlled_access = models.BooleanField(default=True)
    mcp_write_context = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Long-document settings"
        verbose_name_plural = "Long-document settings"

    def __str__(self) -> str:
        return f"Longdoc settings for project {self.project_id}"


class ProjectContextFile(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="context_files")
    filename = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    is_read_only = models.BooleanField(default=True)
    size_bytes = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("project", "filename")]
        ordering = ["project_id", "filename"]

    def __str__(self) -> str:
        return f"{self.project_id}:{self.filename}"


class ProjectOutlineItem(models.Model):
    class Status(models.TextChoices):
        MISSING = "missing", "Missing"
        STUB = "stub", "Stub"
        DRAFT = "draft", "Draft"
        DONE = "done", "Done"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="outline_items")
    order = models.PositiveIntegerField()
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children")
    title = models.CharField(max_length=500)
    level = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.MISSING)
    expected_pages = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["project_id", "order"]
        unique_together = [("project", "order")]
        constraints = [
            models.CheckConstraint(condition=Q(level__gte=1), name="longdoc_outline_level_gte_1"),
        ]

    def __str__(self) -> str:
        return f"{self.project_id}:{self.order}:{self.title}"


class ProjectTask(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In Progress"
        DONE = "done", "Done"

    class CreatedBy(models.TextChoices):
        USER = "user", "User"
        MCP = "mcp", "MCP"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tasks")
    description = models.TextField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    created_by = models.CharField(max_length=20, choices=CreatedBy.choices, default=CreatedBy.USER)
    completed_at = models.DateTimeField(null=True, blank=True)
    ai_session = models.ForeignKey("AISession", null=True, blank=True, on_delete=models.SET_NULL, related_name="completed_tasks")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["project_id", "status", "-created_at"]

    def __str__(self) -> str:
        return f"{self.project_id}:{self.status}:{self.description[:40]}"


class ProjectNoteSection(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="note_sections")
    heading = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["project_id", "order", "id"]
        unique_together = [("project", "heading")]

    def __str__(self) -> str:
        return f"{self.project_id}:{self.heading}"


class SectionSummary(models.Model):
    class WrittenBy(models.TextChoices):
        USER = "user", "User"
        MCP = "mcp", "MCP"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="section_summaries")
    section_title = models.CharField(max_length=500)
    section_index = models.IntegerField(null=True, blank=True)
    source_file = models.CharField(max_length=500)
    source_line_start = models.PositiveIntegerField(null=True, blank=True)
    source_line_end = models.PositiveIntegerField(null=True, blank=True)
    content_hash = models.CharField(max_length=64)
    summary_text = models.TextField()
    written_by = models.CharField(max_length=20, choices=WrittenBy.choices)
    source_version_number = models.PositiveIntegerField()
    is_stale = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("project", "section_title")]
        indexes = [models.Index(fields=["project", "is_stale"])]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(source_line_start__isnull=True)
                    | Q(source_line_end__isnull=True)
                    | Q(source_line_end__gte=models.F("source_line_start"))
                ),
                name="longdoc_summary_line_range_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.project_id}:{self.section_title}"


class ProjectRequirement(models.Model):
    class Coverage(models.TextChoices):
        UNCHECKED = "unchecked", "Unchecked"
        COVERED = "covered", "Covered"
        PARTIAL = "partial", "Partial"
        MISSING = "missing", "Missing"

    class UpdatedBy(models.TextChoices):
        USER = "user", "User"
        MCP = "mcp", "MCP"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="requirements")
    req_id = models.CharField(max_length=50)
    description = models.TextField()
    coverage = models.CharField(max_length=20, choices=Coverage.choices, default=Coverage.UNCHECKED)
    notes = models.TextField(blank=True)
    updated_by = models.CharField(max_length=20, choices=UpdatedBy.choices, default=UpdatedBy.USER)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["project_id", "req_id"]
        unique_together = [("project", "req_id")]

    def __str__(self) -> str:
        return f"{self.project_id}:{self.req_id}"


class RequirementSectionRef(models.Model):
    requirement = models.ForeignKey(ProjectRequirement, on_delete=models.CASCADE, related_name="section_refs")
    section_title = models.CharField(max_length=500)
    outline_item = models.ForeignKey(ProjectOutlineItem, null=True, blank=True, on_delete=models.SET_NULL, related_name="requirement_refs")

    class Meta:
        unique_together = [("requirement", "section_title")]
        indexes = [models.Index(fields=["section_title"])]

    def __str__(self) -> str:
        return f"{self.requirement_id}:{self.section_title}"


class AISession(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        COMPILED = "compiled", "Compiled"
        READY_FOR_REVIEW = "ready_for_review", "Ready for Review"
        ACCEPTED = "accepted", "Accepted"
        DISCARDED = "discarded", "Discarded"
        EXPIRED = "expired", "Expired"

    class CompileStatus(models.TextChoices):
        NOT_RUN = "not_run", "Not Run"
        SUCCESS = "success", "Success"
        ERROR = "error", "Error"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="ai_sessions")
    goal = models.TextField()
    branch_name = models.CharField(max_length=255)
    worktree_path = models.CharField(max_length=500)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.ACTIVE)
    compile_status = models.CharField(max_length=20, choices=CompileStatus.choices, default=CompileStatus.NOT_RUN)
    compile_log = models.TextField(blank=True)
    staging_pdf_path = models.CharField(max_length=500, blank=True)
    diff_text = models.TextField(blank=True)
    created_by_scope = models.CharField(max_length=50, default="mcp")
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    discarded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["project", "status"]),
            models.Index(fields=["expires_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["project"],
                condition=Q(status__in=LOCKING_AI_SESSION_STATUSES),
                name="longdoc_one_locking_session_per_project",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.project_id}:{self.status}:{self.branch_name}"

    @classmethod
    def locking_statuses(cls) -> tuple[str, ...]:
        return LOCKING_AI_SESSION_STATUSES


class ChangeProposal(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        VALIDATING = "validating", "Validating"
        FAILED_VALIDATION = "failed_validation", "Failed Validation"
        FAILED_COMPILE = "failed_compile", "Failed Compile"
        READY_FOR_REVIEW = "ready_for_review", "Ready for Review"
        ACCEPTED = "accepted", "Accepted"
        DISCARDED = "discarded", "Discarded"
        EXPIRED = "expired", "Expired"

    class ValidationStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        PASSED = "passed", "Passed"
        FAILED = "failed", "Failed"

    class CreatedBy(models.TextChoices):
        MCP = "mcp", "MCP"
        USER = "user", "User"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="change_proposals")
    goal = models.TextField()
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.VALIDATING)
    validation_status = models.CharField(max_length=20, choices=ValidationStatus.choices, default=ValidationStatus.PENDING)
    compile_status = models.CharField(max_length=20, choices=AISession.CompileStatus.choices, default=AISession.CompileStatus.NOT_RUN)
    compile_error_summary = models.TextField(blank=True)
    graph_validation_errors = models.JSONField(default=list, blank=True)
    user_visible_message = models.TextField(blank=True)
    patch_ops = models.JSONField(default=list, blank=True)
    changed_files = models.JSONField(default=list, blank=True)
    diff_summary = models.TextField(blank=True)
    smcl_risk_level = models.CharField(max_length=20, blank=True, default="")
    smcl_warnings = models.JSONField(default=list, blank=True)
    smcl_metadata = models.JSONField(default=dict, blank=True)
    addresses_outline_item = models.ForeignKey(
        ProjectOutlineItem,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="change_proposals",
    )
    addresses_task = models.ForeignKey(
        ProjectTask,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="change_proposals",
    )
    internal_session = models.OneToOneField(
        AISession,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="change_proposal",
    )
    created_by = models.CharField(max_length=20, choices=CreatedBy.choices, default=CreatedBy.MCP)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    discarded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["project", "status"]),
            models.Index(fields=["expires_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["project"],
                condition=Q(status__in=LOCKING_CHANGE_PROPOSAL_STATUSES),
                name="longdoc_one_locking_change_proposal_per_project",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.project_id}:{self.status}:{self.goal[:40]}"

    @classmethod
    def locking_statuses(cls) -> tuple[str, ...]:
        return LOCKING_CHANGE_PROPOSAL_STATUSES


class AIBatch(models.Model):
    session = models.OneToOneField(AISession, on_delete=models.CASCADE, related_name="batch")
    summary = models.TextField()
    tasks_completed = models.ManyToManyField(ProjectTask, blank=True, related_name="completed_in_batches")
    notes_updated = models.BooleanField(default=False)
    requirements_updated = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Batch for session {self.session_id}"


class AIBatchChange(models.Model):
    class ChangeType(models.TextChoices):
        MODIFY = "modify", "Modified"
        CREATE = "create", "Created"
        DELETE = "delete", "Deleted"

    batch = models.ForeignKey(AIBatch, on_delete=models.CASCADE, related_name="changes")
    filename = models.CharField(max_length=500)
    change_type = models.CharField(max_length=20, choices=ChangeType.choices)
    diff_text = models.TextField(blank=True)
    lines_added = models.IntegerField(default=0)
    lines_removed = models.IntegerField(default=0)

    class Meta:
        ordering = ["batch_id", "filename"]

    def __str__(self) -> str:
        return f"{self.batch_id}:{self.filename}"


class AssistantAuditLog(models.Model):
    class Source(models.TextChoices):
        USER = "user", "User"
        MCP = "mcp", "MCP"

    class Operation(models.TextChoices):
        CREATE = "create", "Create"
        UPDATE = "update", "Update"
        DELETE = "delete", "Delete"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="assistant_audit_log")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    source = models.CharField(max_length=20, choices=Source.choices)
    model_name = models.CharField(max_length=100)
    object_id = models.PositiveIntegerField()
    operation = models.CharField(max_length=20, choices=Operation.choices)
    changed_fields = models.JSONField(default=dict, blank=True)
    summary = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["project", "-created_at"]),
            models.Index(fields=["model_name", "object_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.project_id}:{self.model_name}:{self.operation}:{self.object_id}"
