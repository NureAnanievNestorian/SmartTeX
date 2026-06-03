from __future__ import annotations

from django.db import models

from projects.models import Project


NAV_SCHEMA_VERSION = 2


class IndexStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    BUILDING = "building", "Building"
    READY = "ready", "Ready"
    PARTIAL = "partial", "Partial"
    FAILED = "failed", "Failed"


class FileRole(models.TextChoices):
    ENTRYPOINT = "entrypoint", "Entrypoint"
    CONTENT_SECTION = "content_section", "Content section"
    METADATA = "metadata", "Metadata"
    STYLE = "style", "Style"
    CLASS = "class", "Class"
    BIB = "bib", "Bibliography"
    CSL = "csl", "CSL"
    CONFIG = "config", "Config"
    ASSET_METADATA = "asset_metadata", "Asset metadata"
    AUXILIARY = "auxiliary", "Auxiliary"
    UNKNOWN = "unknown", "Unknown"


class StateKind(models.TextChoices):
    REAL = "real", "Real"
    DEMO = "demo", "Demo"
    PLACEHOLDER = "placeholder", "Placeholder"
    UNKNOWN = "unknown", "Unknown"


class Reachability(models.TextChoices):
    REACHABLE = "reachable", "Reachable"
    ORPHAN = "orphan", "Orphan"
    MISSING = "missing", "Missing"
    DYNAMIC_UNRESOLVED = "dynamic_unresolved", "Dynamic unresolved"
    EXCLUDED = "excluded", "Excluded"


class Source(models.TextChoices):
    DETERMINISTIC = "deterministic", "Deterministic"
    SMALL_MODEL = "small_model", "Small model"


class Confidence(models.TextChoices):
    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"


class RegionKind(models.TextChoices):
    HEADING_SECTION = "heading_section", "Heading section"
    METADATA_BLOCK = "metadata_block", "Metadata block"
    FRONT_MATTER = "front_matter", "Front matter"
    BIBLIOGRAPHY_BLOCK = "bibliography_block", "Bibliography block"
    CODE_BLOCK = "code_block", "Code block"
    FIGURE_BLOCK = "figure_block", "Figure block"
    UNKNOWN = "unknown", "Unknown"


class ProjectNavigationIndex(models.Model):
    project = models.OneToOneField(
        Project, on_delete=models.CASCADE, related_name="navigation_index"
    )
    schema_version = models.PositiveSmallIntegerField(default=NAV_SCHEMA_VERSION)
    status = models.CharField(
        max_length=16, choices=IndexStatus.choices, default=IndexStatus.PENDING
    )
    markup_type_snapshot = models.CharField(max_length=16, blank=True, default="")
    main_file_snapshot = models.CharField(max_length=255, blank=True, default="")
    entrypoint_file = models.CharField(max_length=255, blank=True, default="")
    last_built_at = models.DateTimeField(null=True, blank=True)
    last_built_version_number = models.PositiveIntegerField(default=0)
    last_partial_refresh_at = models.DateTimeField(null=True, blank=True)
    coverage = models.JSONField(default=dict, blank=True)
    build_error = models.TextField(blank=True, default="")
    notes = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"NavIndex[{self.project_id}] {self.status}"


class FileCard(models.Model):
    index = models.ForeignKey(
        ProjectNavigationIndex, on_delete=models.CASCADE, related_name="file_cards"
    )
    filename = models.CharField(max_length=512)
    role = models.CharField(max_length=24, choices=FileRole.choices, default=FileRole.UNKNOWN)
    role_source = models.CharField(
        max_length=16, choices=Source.choices, default=Source.DETERMINISTIC
    )
    role_confidence = models.CharField(
        max_length=8, choices=Confidence.choices, default=Confidence.LOW
    )
    state = models.CharField(max_length=16, choices=StateKind.choices, default=StateKind.UNKNOWN)
    state_source = models.CharField(
        max_length=16, choices=Source.choices, default=Source.DETERMINISTIC
    )
    state_confidence = models.CharField(
        max_length=8, choices=Confidence.choices, default=Confidence.LOW
    )
    reachability = models.CharField(
        max_length=24, choices=Reachability.choices, default=Reachability.ORPHAN
    )
    included_by_filenames = models.JSONField(default=list, blank=True)
    includes_out_filenames = models.JSONField(default=list, blank=True)
    summary = models.CharField(max_length=280, blank=True, default="")
    summary_source = models.CharField(
        max_length=16, choices=Source.choices, default=Source.DETERMINISTIC
    )
    summary_confidence = models.CharField(
        max_length=8, choices=Confidence.choices, default=Confidence.LOW
    )
    edit_triggers = models.JSONField(default=list, blank=True)
    triggers_source = models.CharField(
        max_length=16, choices=Source.choices, default=Source.DETERMINISTIC
    )
    line_count = models.PositiveIntegerField(default=0)
    byte_size = models.PositiveIntegerField(default=0)
    content_hash = models.CharField(max_length=64, blank=True, default="")
    last_version_number = models.PositiveIntegerField(default=0)
    last_indexed_at = models.DateTimeField(null=True, blank=True)
    is_stale = models.BooleanField(default=False)
    exclusion_reason = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("index", "filename")]
        indexes = [
            models.Index(fields=["index", "is_stale"]),
            models.Index(fields=["index", "role"]),
            models.Index(fields=["index", "reachability"]),
            models.Index(fields=["index", "content_hash"]),
        ]

    def __str__(self) -> str:
        return f"FileCard[{self.filename}]"


class RegionCard(models.Model):
    file_card = models.ForeignKey(
        FileCard, on_delete=models.CASCADE, related_name="region_cards"
    )
    region_kind = models.CharField(
        max_length=24, choices=RegionKind.choices, default=RegionKind.UNKNOWN
    )
    title = models.CharField(max_length=512, blank=True, default="")
    level = models.PositiveSmallIntegerField(null=True, blank=True)
    order = models.PositiveIntegerField(default=0)
    line_start = models.PositiveIntegerField(default=1)
    line_end = models.PositiveIntegerField(default=1)
    state = models.CharField(max_length=16, choices=StateKind.choices, default=StateKind.UNKNOWN)
    state_source = models.CharField(
        max_length=16, choices=Source.choices, default=Source.DETERMINISTIC
    )
    state_confidence = models.CharField(
        max_length=8, choices=Confidence.choices, default=Confidence.LOW
    )
    summary = models.CharField(max_length=280, blank=True, default="")
    summary_source = models.CharField(
        max_length=16, choices=Source.choices, default=Source.DETERMINISTIC
    )
    summary_confidence = models.CharField(
        max_length=8, choices=Confidence.choices, default=Confidence.LOW
    )
    edit_triggers = models.JSONField(default=list, blank=True)
    triggers_source = models.CharField(
        max_length=16, choices=Source.choices, default=Source.DETERMINISTIC
    )
    content_hash = models.CharField(max_length=64, blank=True, default="")
    last_indexed_at = models.DateTimeField(null=True, blank=True)
    is_stale = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("file_card", "order")]
        indexes = [
            models.Index(fields=["file_card", "is_stale"]),
            models.Index(fields=["file_card", "region_kind"]),
            models.Index(fields=["file_card", "content_hash"]),
        ]
        ordering = ["file_card_id", "order"]

    def __str__(self) -> str:
        return f"RegionCard[{self.file_card_id}#{self.order} {self.title[:40]}]"
