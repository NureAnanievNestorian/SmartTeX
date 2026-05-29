from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from projects.models import ProjectVersion
from projects.services import (
    ALLOWED_UPLOAD_EXTENSIONS,
    TEXT_EXTENSIONS,
    create_text_project_version,
    ensure_project_dir,
    list_source_sections,
    main_source_filename,
    project_asset_path,
    project_dir,
    source_file_path,
)

from .audit import create_assistant_audit_log, diff_model_snapshots, serialize_model_instance
from .locks import ProjectLockedError, assert_not_locked
from .models import (
    AssistantAuditLog,
    ProjectContextFile,
    ProjectLongDocSettings,
    ProjectNoteSection,
    ProjectOutlineItem,
    ProjectRequirement,
    ProjectTask,
    RequirementSectionRef,
    SectionSummary,
)


DEFAULT_NOTE_SECTION_HEADINGS = (
    "Writing Decisions",
    "Terminology",
    "Unresolved Questions",
    "Things Not to Change",
    "Session Progress",
)
SAMPLE_CONTEXT_FILENAME = "project-brief.md"
SAMPLE_OUTLINE_ITEMS = (
    {"title": "Introduction", "level": 1, "status": ProjectOutlineItem.Status.STUB},
    {"title": "Core Argument", "level": 1, "status": ProjectOutlineItem.Status.MISSING},
    {"title": "Conclusion", "level": 1, "status": ProjectOutlineItem.Status.MISSING},
)
SAMPLE_TASKS = (
    "Confirm the target reader and document goal.",
    "Expand the outline into the main milestones for the next draft.",
)


@dataclass
class LongdocAccessError(RuntimeError):
    error: str
    message: str
    status_code: int = 400
    suggestion: str = ""
    extra: dict[str, Any] | None = None

    def payload(self) -> dict[str, Any]:
        payload = {
            "error": self.error,
            "detail": self.message,
        }
        if self.suggestion:
            payload["suggestion"] = self.suggestion
        if self.extra:
            payload.update(self.extra)
        return payload


def longdoc_default_settings() -> dict[str, bool]:
    defaults = getattr(settings, "LONGDOC_DEFAULTS", {})
    return {
        "enabled": bool(defaults.get("enabled", False)),
        "context_enabled": bool(defaults.get("context_enabled", True)),
        "outline_enabled": bool(defaults.get("outline_enabled", True)),
        "tasks_enabled": bool(defaults.get("tasks_enabled", True)),
        "notes_enabled": bool(defaults.get("notes_enabled", True)),
        "summaries_enabled": bool(defaults.get("summaries_enabled", True)),
        "requirements_enabled": bool(defaults.get("requirements_enabled", False)),
        "ai_sessions_enabled": bool(defaults.get("ai_sessions_enabled", True)),
        "mcp_controlled_access": bool(defaults.get("mcp_controlled_access", True)),
        "mcp_write_context": bool(defaults.get("mcp_write_context", False)),
    }


def get_longdoc_settings_or_none(project):
    return ProjectLongDocSettings.objects.filter(project=project).first()


def longdoc_context_dir(project) -> Path:
    return project_dir(project) / ".smarttex" / "context"


def ensure_context_dir(project) -> Path:
    root = longdoc_context_dir(project)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sample_context_content(project) -> str:
    return (
        f"# {project.title or 'Project'} brief\n\n"
        "Use this file to capture the document goal, target reader, and non-negotiable constraints.\n\n"
        "## Goals\n"
        "- \n\n"
        "## Constraints\n"
        "- \n\n"
        "## Reference notes\n"
        "- \n"
    )


def ensure_default_note_sections(project) -> None:
    existing = set(
        ProjectNoteSection.objects.filter(project=project, heading__in=DEFAULT_NOTE_SECTION_HEADINGS)
        .values_list("heading", flat=True)
    )
    to_create = []
    for order, heading in enumerate(DEFAULT_NOTE_SECTION_HEADINGS):
        if heading not in existing:
            to_create.append(ProjectNoteSection(project=project, heading=heading, order=order))
    if to_create:
        ProjectNoteSection.objects.bulk_create(to_create)


def ensure_sample_context_files(project) -> None:
    ensure_context_dir(project)
    existing = sync_context_file_records(project)
    if existing:
        return
    create_context_file(
        project,
        filename=SAMPLE_CONTEXT_FILENAME,
        content=_sample_context_content(project),
        display_name="Project brief",
        description="Starter context file for goals, constraints, and reference notes.",
        actor=None,
        source="api",
        summary="Initialized long-document context",
        is_read_only=False,
    )


def ensure_sample_outline_items(project) -> None:
    if ProjectOutlineItem.objects.filter(project=project).exists():
        return
    ProjectOutlineItem.objects.bulk_create(
        [
            ProjectOutlineItem(
                project=project,
                order=index,
                title=item["title"],
                level=item["level"],
                status=item["status"],
            )
            for index, item in enumerate(SAMPLE_OUTLINE_ITEMS, start=1)
        ]
    )


def ensure_sample_tasks(project) -> None:
    if ProjectTask.objects.filter(project=project).exists():
        return
    ProjectTask.objects.bulk_create(
        [
            ProjectTask(
                project=project,
                description=description,
                created_by=ProjectTask.CreatedBy.USER,
            )
            for description in SAMPLE_TASKS
        ]
    )


def ensure_longdoc_seed_data(project) -> None:
    ensure_context_dir(project)
    ensure_default_note_sections(project)
    ensure_sample_context_files(project)
    ensure_sample_outline_items(project)
    ensure_sample_tasks(project)


def get_or_create_longdoc_settings(project) -> tuple[ProjectLongDocSettings, bool]:
    settings_obj, created = ProjectLongDocSettings.objects.get_or_create(
        project=project,
        defaults=longdoc_default_settings(),
    )
    if settings_obj.enabled:
        ensure_longdoc_seed_data(project)
    return settings_obj, created


def is_feature_enabled(project_or_settings, feature_name: str) -> bool:
    settings_obj = (
        project_or_settings
        if isinstance(project_or_settings, ProjectLongDocSettings)
        else get_longdoc_settings_or_none(project_or_settings)
    )
    if settings_obj is None:
        return False
    if feature_name == "enabled":
        return settings_obj.enabled
    if not hasattr(settings_obj, feature_name):
        raise AttributeError(f"Unknown longdoc feature: {feature_name}")
    return bool(settings_obj.enabled and getattr(settings_obj, feature_name))


def enable_longdoc(project, **overrides) -> ProjectLongDocSettings:
    settings_obj, _ = get_or_create_longdoc_settings(project)
    fields_to_update = []
    if not settings_obj.enabled:
        settings_obj.enabled = True
        fields_to_update.append("enabled")
    for field_name, value in overrides.items():
        if not hasattr(settings_obj, field_name):
            raise AttributeError(f"Unknown longdoc setting: {field_name}")
        if getattr(settings_obj, field_name) != value:
            setattr(settings_obj, field_name, value)
            fields_to_update.append(field_name)
    if fields_to_update:
        settings_obj.save(update_fields=[*fields_to_update, "updated_at"])
    ensure_longdoc_seed_data(project)
    return settings_obj


def disable_longdoc(project) -> ProjectLongDocSettings:
    settings_obj, _ = get_or_create_longdoc_settings(project)
    if settings_obj.enabled:
        settings_obj.enabled = False
        settings_obj.save(update_fields=["enabled", "updated_at"])
    return settings_obj


def update_longdoc_settings(project, **changes) -> ProjectLongDocSettings:
    settings_obj, _ = get_or_create_longdoc_settings(project)
    mutable_fields = {
        "enabled",
        "context_enabled",
        "outline_enabled",
        "tasks_enabled",
        "notes_enabled",
        "summaries_enabled",
        "requirements_enabled",
        "ai_sessions_enabled",
        "mcp_controlled_access",
        "mcp_write_context",
    }
    dirty_fields: list[str] = []
    for field_name, value in changes.items():
        if field_name not in mutable_fields:
            raise AttributeError(f"Unknown longdoc setting: {field_name}")
        normalized = bool(value)
        if getattr(settings_obj, field_name) != normalized:
            setattr(settings_obj, field_name, normalized)
            dirty_fields.append(field_name)
    if dirty_fields:
        settings_obj.save(update_fields=[*dirty_fields, "updated_at"])
    if settings_obj.enabled:
        ensure_longdoc_seed_data(project)
    return settings_obj


def update_small_model_settings(project, **changes):
    from small_model.models import ProjectSmallModelSettings

    settings_obj, _ = ProjectSmallModelSettings.objects.get_or_create(project=project)
    mutable_fields = {
        "small_model_control_enabled",
        "context_compressor_enabled",
        "edit_intent_classifier_enabled",
        "diff_safety_reviewer_enabled",
        "compile_log_triage_enabled",
        "circuit_breaker_enabled",
    }
    dirty_fields: list[str] = []
    for field_name, value in changes.items():
        if field_name not in mutable_fields:
            raise AttributeError(f"Unknown small model setting: {field_name}")
        normalized = bool(value)
        if getattr(settings_obj, field_name) != normalized:
            setattr(settings_obj, field_name, normalized)
            dirty_fields.append(field_name)
    if dirty_fields:
        settings_obj.save(update_fields=[*dirty_fields, "updated_at"])
    return settings_obj


def serialize_settings(
    settings_obj: ProjectLongDocSettings,
    *,
    locked: bool = False,
    locking_session_id: int | None = None,
    locking_proposal_id: int | None = None,
) -> dict[str, Any]:
    from small_model.models import ProjectSmallModelSettings

    smcl = ProjectSmallModelSettings.objects.filter(project=settings_obj.project).first()
    return {
        "enabled": settings_obj.enabled,
        "context_enabled": settings_obj.context_enabled,
        "outline_enabled": settings_obj.outline_enabled,
        "tasks_enabled": settings_obj.tasks_enabled,
        "notes_enabled": settings_obj.notes_enabled,
        "summaries_enabled": settings_obj.summaries_enabled,
        "requirements_enabled": settings_obj.requirements_enabled,
        "ai_sessions_enabled": settings_obj.ai_sessions_enabled,
        "mcp_controlled_access": settings_obj.mcp_controlled_access,
        "mcp_write_context": settings_obj.mcp_write_context,
        "locked": bool(locked),
        "locking_session_id": locking_session_id,
        "locking_proposal_id": locking_proposal_id,
        "small_model": {
            "small_model_control_enabled": bool(smcl and smcl.small_model_control_enabled),
            "context_compressor_enabled": bool(smcl and smcl.context_compressor_enabled),
            "edit_intent_classifier_enabled": bool(smcl and smcl.edit_intent_classifier_enabled),
            "diff_safety_reviewer_enabled": bool(smcl and smcl.diff_safety_reviewer_enabled),
            "compile_log_triage_enabled": bool(smcl and smcl.compile_log_triage_enabled),
            "circuit_breaker_enabled": bool(smcl and smcl.circuit_breaker_enabled),
        },
        "updated_at": settings_obj.updated_at.isoformat(),
    }


def _safe_context_rel_path(filename: str) -> Path:
    raw = str(filename or "").strip().replace("\\", "/").lstrip("/")
    if not raw:
        raise ValueError("filename is required")
    rel = Path(raw)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError("invalid context filename")
    if any(part.startswith(".") for part in rel.parts):
        raise ValueError("hidden context paths are not allowed")
    ext = rel.suffix.lower()
    if ext and ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise ValueError(f"unsupported file extension: {ext}")
    return rel


def context_file_path(project, filename: str) -> Path:
    rel = _safe_context_rel_path(filename)
    root = ensure_context_dir(project).resolve()
    target = (root / rel).resolve()
    if root != target and root not in target.parents:
        raise ValueError("path escapes context directory")
    return target


def _context_display_name(filename: str) -> str:
    stem = Path(filename).stem.replace("-", " ").replace("_", " ").strip()
    return stem.title() if stem else filename


def sync_context_file_records(project) -> list[ProjectContextFile]:
    root = ensure_context_dir(project)
    disk_files: dict[str, Path] = {}
    for path in sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root)).lower()):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        disk_files[rel] = path

    existing = {row.filename: row for row in ProjectContextFile.objects.filter(project=project)}
    records: list[ProjectContextFile] = []

    for filename, path in disk_files.items():
        row = existing.pop(filename, None)
        payload = {
            "display_name": row.display_name if row and row.display_name else _context_display_name(filename),
            "description": row.description if row else "",
            "is_read_only": bool(row.is_read_only) if row else True,
            "size_bytes": path.stat().st_size,
        }
        if row is None:
            row = ProjectContextFile.objects.create(project=project, filename=filename, **payload)
        else:
            dirty = []
            for field_name, value in payload.items():
                if getattr(row, field_name) != value:
                    setattr(row, field_name, value)
                    dirty.append(field_name)
            if dirty:
                row.save(update_fields=[*dirty, "updated_at"])
        records.append(row)

    if existing:
        ProjectContextFile.objects.filter(id__in=[item.id for item in existing.values()]).delete()

    return sorted(records, key=lambda item: item.filename.lower())


def serialize_context_file(item: ProjectContextFile, *, include_content: bool = False) -> dict[str, Any]:
    payload = {
        "id": item.id,
        "filename": item.filename,
        "display_name": item.display_name,
        "description": item.description,
        "is_read_only": item.is_read_only,
        "size_bytes": item.size_bytes,
        "updated_at": item.updated_at.isoformat(),
    }
    if include_content:
        path = context_file_path(item.project, item.filename)
        ext = path.suffix.lower()
        payload["is_text"] = ext in TEXT_EXTENSIONS
        if ext in TEXT_EXTENSIONS and path.exists():
            payload["content"] = path.read_text(encoding="utf-8", errors="ignore")
        else:
            payload["content"] = None
    return payload


def list_context_files(project) -> list[dict[str, Any]]:
    return [serialize_context_file(item) for item in sync_context_file_records(project)]


def get_context_file(project, filename: str, *, include_content: bool = True) -> dict[str, Any]:
    sync_context_file_records(project)
    item = ProjectContextFile.objects.filter(project=project, filename=str(filename).strip()).first()
    if item is None:
        raise ValueError("context file not found")
    return serialize_context_file(item, include_content=include_content)


def _maybe_create_assistant_version(project, actor, source: str, filename: str, summary: str) -> None:
    if source != "mcp":
        return
    create_text_project_version(
        project=project,
        actor=actor,
        source=source,
        operation="update_context_file",
        target=f".smarttex/context/{filename}",
        target_file=f".smarttex/context/{filename}",
        summary=summary,
        tracked_files=[f".smarttex/context/{filename}"],
        category=ProjectVersion.Category.ASSISTANT,
    )


def create_context_file(
    project,
    *,
    filename: str,
    content: str,
    description: str = "",
    display_name: str = "",
    actor=None,
    source: str = "web",
    summary: str = "",
    is_read_only: bool = False,
) -> dict[str, Any]:
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    path = context_file_path(project, filename)
    if path.exists():
        raise ValueError("context file already exists")
    ensure_context_dir(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    item = ProjectContextFile.objects.create(
        project=project,
        filename=str(path.relative_to(longdoc_context_dir(project).resolve())).replace("\\", "/"),
        display_name=display_name.strip() or _context_display_name(filename),
        description=description.strip(),
        is_read_only=bool(is_read_only),
        size_bytes=path.stat().st_size,
    )
    _maybe_create_assistant_version(
        project,
        actor,
        source,
        item.filename,
        summary or f"Updated context file {item.filename}",
    )
    return serialize_context_file(item, include_content=True)


def update_context_file(
    project,
    *,
    filename: str,
    actor=None,
    source: str = "web",
    summary: str = "",
    content: str | None = None,
    description: str | None = None,
    display_name: str | None = None,
    is_read_only: bool | None = None,
    create_if_missing: bool = False,
) -> dict[str, Any]:
    sync_context_file_records(project)
    item = ProjectContextFile.objects.filter(project=project, filename=filename).first()
    if item is None:
        if not create_if_missing:
            raise ValueError("context file not found")
        return create_context_file(
            project,
            filename=filename,
            content=content or "",
            description=description or "",
            display_name=display_name or "",
            actor=actor,
            source=source,
            summary=summary,
            is_read_only=bool(is_read_only) if is_read_only is not None else False,
        )

    path = context_file_path(project, item.filename)
    content_changed = False
    if content is not None:
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        path.write_text(content, encoding="utf-8")
        item.size_bytes = path.stat().st_size
        content_changed = True

    dirty_fields: list[str] = []
    if description is not None and item.description != description:
        item.description = description
        dirty_fields.append("description")
    if display_name is not None and item.display_name != display_name:
        item.display_name = display_name
        dirty_fields.append("display_name")
    if is_read_only is not None and item.is_read_only != bool(is_read_only):
        item.is_read_only = bool(is_read_only)
        dirty_fields.append("is_read_only")
    if content_changed:
        dirty_fields.append("size_bytes")
    if dirty_fields:
        item.save(update_fields=[*dirty_fields, "updated_at"])
    if content_changed:
        _maybe_create_assistant_version(
            project,
            actor,
            source,
            item.filename,
            summary or f"Updated context file {item.filename}",
        )
    return serialize_context_file(item, include_content=True)


def delete_context_file(project, *, filename: str, actor=None, source: str = "web", summary: str = "") -> None:
    sync_context_file_records(project)
    item = ProjectContextFile.objects.filter(project=project, filename=filename).first()
    if item is None:
        raise ValueError("context file not found")
    path = context_file_path(project, item.filename)
    if path.exists():
        path.unlink()
    item.delete()
    _maybe_create_assistant_version(
        project,
        actor,
        source,
        filename,
        summary or f"Deleted context file {filename}",
    )


def serialize_outline_item(item: ProjectOutlineItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "order": item.order,
        "parent_id": item.parent_id,
        "title": item.title,
        "level": item.level,
        "status": item.status,
        "expected_pages": float(item.expected_pages) if item.expected_pages is not None else None,
        "notes": item.notes,
        "updated_at": item.updated_at.isoformat(),
    }


def _audit_db_change(instance, *, operation: str, actor, source: str, summary: str, before_snapshot=None, object_id: int | None = None) -> None:
    if source != "mcp":
        return
    after_snapshot = None if operation == AssistantAuditLog.Operation.DELETE else serialize_model_instance(instance)
    create_assistant_audit_log(
        instance=instance,
        operation=operation,
        source=AssistantAuditLog.Source.MCP,
        actor=actor,
        summary=summary,
        before=before_snapshot,
        after=after_snapshot,
        changed_fields=diff_model_snapshots(before_snapshot, after_snapshot),
        object_id=object_id,
    )


def list_outline_items(project) -> list[dict[str, Any]]:
    return [serialize_outline_item(item) for item in ProjectOutlineItem.objects.filter(project=project).order_by("order", "id")]


def _resequence_outline(project, ordered_ids: list[int]) -> None:
    items_by_id = {item.id: item for item in ProjectOutlineItem.objects.filter(project=project)}
    temp_items = []
    for offset, item_id in enumerate(sorted(items_by_id), start=1):
        item = items_by_id[item_id]
        item.order = 1000 + offset
        temp_items.append(item)
    if temp_items:
        ProjectOutlineItem.objects.bulk_update(temp_items, ["order"])
    updated = []
    for index, item_id in enumerate(ordered_ids, start=1):
        item = items_by_id[item_id]
        item.order = index
        updated.append(item)
    if updated:
        ProjectOutlineItem.objects.bulk_update(updated, ["order"])


def create_outline_item(
    project,
    *,
    actor=None,
    source: str = "web",
    summary: str = "",
    title: str,
    level: int = 1,
    status: str = ProjectOutlineItem.Status.MISSING,
    order: int | None = None,
    notes: str = "",
    expected_pages=None,
    parent_id: int | None = None,
) -> dict[str, Any]:
    if not title.strip():
        raise ValueError("title is required")
    parent = None
    if parent_id is not None:
        parent = ProjectOutlineItem.objects.filter(project=project, id=parent_id).first()
        if parent is None:
            raise ValueError("parent outline item not found")
    with transaction.atomic():
        max_order = ProjectOutlineItem.objects.filter(project=project).aggregate(value=Max("order")).get("value") or 0
        item = ProjectOutlineItem.objects.create(
            project=project,
            order=max_order + 1,
            parent=parent,
            title=title.strip(),
            level=max(1, int(level)),
            status=status,
            expected_pages=expected_pages,
            notes=notes,
        )
        if order is not None:
            desired = max(1, min(int(order), max_order + 1))
            ordered_ids = list(
                ProjectOutlineItem.objects.filter(project=project).order_by("order", "id").values_list("id", flat=True)
            )
            ordered_ids.remove(item.id)
            ordered_ids.insert(desired - 1, item.id)
            _resequence_outline(project, ordered_ids)
            item.refresh_from_db()
    _audit_db_change(
        item,
        operation=AssistantAuditLog.Operation.CREATE,
        actor=actor,
        source=source,
        summary=summary or f"Created outline item {item.title}",
        before_snapshot=None,
    )
    return serialize_outline_item(item)


def update_outline_item(
    project,
    *,
    item_id: int,
    actor=None,
    source: str = "web",
    summary: str = "",
    **changes,
) -> dict[str, Any]:
    with transaction.atomic():
        item = ProjectOutlineItem.objects.select_for_update().get(project=project, id=item_id)
        before = serialize_model_instance(item)
        if "parent_id" in changes:
            parent_id = changes.pop("parent_id")
            if parent_id is None:
                item.parent = None
            else:
                item.parent = ProjectOutlineItem.objects.filter(project=project, id=parent_id).first()
                if item.parent is None:
                    raise ValueError("parent outline item not found")
        if "title" in changes:
            item.title = str(changes.pop("title")).strip()
            if not item.title:
                raise ValueError("title is required")
        if "level" in changes:
            item.level = max(1, int(changes.pop("level")))
        if "status" in changes:
            item.status = changes.pop("status")
        if "notes" in changes:
            item.notes = str(changes.pop("notes") or "")
        if "expected_pages" in changes:
            item.expected_pages = changes.pop("expected_pages")
        target_order = changes.pop("order", None)
        if changes:
            raise ValueError(f"unsupported outline changes: {', '.join(sorted(changes))}")
        item.save()
        if target_order is not None:
            ordered_ids = list(
                ProjectOutlineItem.objects.filter(project=project).order_by("order", "id").values_list("id", flat=True)
            )
            ordered_ids.remove(item.id)
            ordered_ids.insert(max(0, min(int(target_order) - 1, len(ordered_ids))), item.id)
            _resequence_outline(project, ordered_ids)
            item.refresh_from_db()
    _audit_db_change(
        item,
        operation=AssistantAuditLog.Operation.UPDATE,
        actor=actor,
        source=source,
        summary=summary or f"Updated outline item {item.title}",
        before_snapshot=before,
    )
    return serialize_outline_item(item)


def delete_outline_item(project, *, item_id: int, actor=None, source: str = "web", summary: str = "") -> None:
    with transaction.atomic():
        item = ProjectOutlineItem.objects.select_for_update().get(project=project, id=item_id)
        before = serialize_model_instance(item)
        object_id = item.id
        item.delete()
        ordered_ids = list(
            ProjectOutlineItem.objects.filter(project=project).order_by("order", "id").values_list("id", flat=True)
        )
        _resequence_outline(project, ordered_ids)
    phantom = ProjectOutlineItem(project=project, title=before.get("title", ""), level=before.get("level", 1), order=before.get("order", 0))
    _audit_db_change(
        phantom,
        operation=AssistantAuditLog.Operation.DELETE,
        actor=actor,
        source=source,
        summary=summary or f"Deleted outline item {before.get('title', '')}".strip(),
        before_snapshot=before,
        object_id=object_id,
    )


def serialize_task(item: ProjectTask) -> dict[str, Any]:
    return {
        "id": item.id,
        "description": item.description,
        "status": item.status,
        "created_by": item.created_by,
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
        "updated_at": item.updated_at.isoformat(),
    }


def list_tasks(project) -> list[dict[str, Any]]:
    return [serialize_task(item) for item in ProjectTask.objects.filter(project=project).order_by("status", "-updated_at", "-id")]


def create_task(project, *, description: str, actor=None, source: str = "web", summary: str = "", created_by: str | None = None) -> dict[str, Any]:
    if not description.strip():
        raise ValueError("description is required")
    item = ProjectTask.objects.create(
        project=project,
        description=description.strip(),
        created_by=created_by or (ProjectTask.CreatedBy.MCP if source == "mcp" else ProjectTask.CreatedBy.USER),
    )
    _audit_db_change(
        item,
        operation=AssistantAuditLog.Operation.CREATE,
        actor=actor,
        source=source,
        summary=summary or f"Created task {item.description[:80]}",
    )
    return serialize_task(item)


def update_task(project, *, task_id: int, actor=None, source: str = "web", summary: str = "", **changes) -> dict[str, Any]:
    item = ProjectTask.objects.get(project=project, id=task_id)
    before = serialize_model_instance(item)
    if "description" in changes:
        item.description = str(changes.pop("description")).strip()
        if not item.description:
            raise ValueError("description is required")
    if "status" in changes:
        status = changes.pop("status")
        item.status = status
        item.completed_at = timezone.now() if status == ProjectTask.Status.DONE else None
    if changes:
        raise ValueError(f"unsupported task changes: {', '.join(sorted(changes))}")
    item.save()
    _audit_db_change(
        item,
        operation=AssistantAuditLog.Operation.UPDATE,
        actor=actor,
        source=source,
        summary=summary or f"Updated task {item.description[:80]}",
        before_snapshot=before,
    )
    return serialize_task(item)


def delete_task(project, *, task_id: int, actor=None, source: str = "web", summary: str = "") -> None:
    item = ProjectTask.objects.get(project=project, id=task_id)
    before = serialize_model_instance(item)
    object_id = item.id
    item.delete()
    phantom = ProjectTask(project=project, description=before.get("description", ""), status=before.get("status", ProjectTask.Status.OPEN))
    _audit_db_change(
        phantom,
        operation=AssistantAuditLog.Operation.DELETE,
        actor=actor,
        source=source,
        summary=summary or f"Deleted task {before.get('description', '')[:80]}",
        before_snapshot=before,
        object_id=object_id,
    )


def serialize_note_section(item: ProjectNoteSection, *, include_preview: bool = False) -> dict[str, Any]:
    payload = {
        "id": item.id,
        "heading": item.heading,
        "body": item.body,
        "order": item.order,
        "updated_at": item.updated_at.isoformat(),
    }
    if include_preview:
        preview = item.body.strip().splitlines()
        payload["preview"] = preview[0][:160] if preview else ""
        payload.pop("body", None)
    return payload


def list_note_sections(project, *, include_preview: bool = False) -> list[dict[str, Any]]:
    return [
        serialize_note_section(item, include_preview=include_preview)
        for item in ProjectNoteSection.objects.filter(project=project).order_by("order", "id")
    ]


def create_note_section(project, *, heading: str, body: str = "", order: int | None = None, actor=None, source: str = "web", summary: str = "") -> dict[str, Any]:
    if not heading.strip():
        raise ValueError("heading is required")
    max_order = ProjectNoteSection.objects.filter(project=project).aggregate(value=Max("order")).get("value") or 0
    item = ProjectNoteSection.objects.create(
        project=project,
        heading=heading.strip(),
        body=body,
        order=max_order + 1 if order is None else max(0, int(order)),
    )
    _audit_db_change(
        item,
        operation=AssistantAuditLog.Operation.CREATE,
        actor=actor,
        source=source,
        summary=summary or f"Created note section {item.heading}",
    )
    return serialize_note_section(item)


def update_note_section(project, *, section_id: int, actor=None, source: str = "web", summary: str = "", **changes) -> dict[str, Any]:
    item = ProjectNoteSection.objects.get(project=project, id=section_id)
    before = serialize_model_instance(item)
    if "heading" in changes:
        item.heading = str(changes.pop("heading")).strip()
        if not item.heading:
            raise ValueError("heading is required")
    if "body" in changes:
        item.body = str(changes.pop("body") or "")
    if "order" in changes:
        item.order = max(0, int(changes.pop("order")))
    if changes:
        raise ValueError(f"unsupported note changes: {', '.join(sorted(changes))}")
    item.save()
    _audit_db_change(
        item,
        operation=AssistantAuditLog.Operation.UPDATE,
        actor=actor,
        source=source,
        summary=summary or f"Updated note section {item.heading}",
        before_snapshot=before,
    )
    return serialize_note_section(item)


def delete_note_section(project, *, section_id: int, actor=None, source: str = "web", summary: str = "") -> None:
    item = ProjectNoteSection.objects.get(project=project, id=section_id)
    before = serialize_model_instance(item)
    object_id = item.id
    item.delete()
    phantom = ProjectNoteSection(project=project, heading=before.get("heading", ""), order=before.get("order", 0))
    _audit_db_change(
        phantom,
        operation=AssistantAuditLog.Operation.DELETE,
        actor=actor,
        source=source,
        summary=summary or f"Deleted note section {before.get('heading', '')}",
        before_snapshot=before,
        object_id=object_id,
    )


def _normalize_summary_source_file(project, source_file: str | None) -> str:
    value = str(source_file or "").strip()
    return value or main_source_filename(project)


def _resolve_project_text_path(project, file_name: str) -> Path:
    normalized = _normalize_summary_source_file(project, file_name)
    if normalized == main_source_filename(project):
        return source_file_path(project)
    return project_asset_path(project, normalized)


def _read_text_line_range(project, source_file: str, start_line: int | None, end_line: int | None) -> str:
    path = _resolve_project_text_path(project, source_file)
    if not path.exists() or not path.is_file():
        raise ValueError(f"source file not found: {source_file}")
    content = path.read_text(encoding="utf-8", errors="ignore")
    if start_line is None or end_line is None:
        return content
    if int(start_line) < 1 or int(end_line) < int(start_line):
        raise ValueError("invalid section line range")
    lines = content.splitlines(keepends=True)
    start_index = max(0, int(start_line) - 1)
    end_index = min(len(lines), int(end_line))
    if start_index >= len(lines) and lines:
        raise ValueError("section start line is out of bounds")
    return "".join(lines[start_index:end_index])


def _hash_text(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _latest_version_number(project, source_file: str) -> int:
    return (
        ProjectVersion.objects.filter(project=project, target_file=source_file)
        .order_by("-number")
        .values_list("number", flat=True)
        .first()
        or 0
    )


def _matching_live_section(project, *, section_title: str, section_index: int | None = None, source_file: str | None = None) -> dict[str, Any] | None:
    normalized_file = _normalize_summary_source_file(project, source_file)
    for item in list_source_sections(project):
        if section_index is not None and int(item.get("index", -1)) == int(section_index):
            if _normalize_summary_source_file(project, str(item.get("file_name") or "")) == normalized_file:
                return item
        if str(item.get("title") or "").strip() == section_title.strip():
            if _normalize_summary_source_file(project, str(item.get("file_name") or "")) == normalized_file:
                return item
    return None


def _resolve_summary_source_metadata(
    project,
    *,
    section_title: str,
    section_index: int | None = None,
    source_file: str | None = None,
    source_line_start: int | None = None,
    source_line_end: int | None = None,
) -> dict[str, Any]:
    normalized_file = _normalize_summary_source_file(project, source_file)
    if source_line_start is not None or source_line_end is not None:
        if source_line_start is None or source_line_end is None:
            raise ValueError("source_line_start and source_line_end must be provided together")
        return {
            "section_title": section_title.strip(),
            "section_index": section_index,
            "source_file": normalized_file,
            "source_line_start": int(source_line_start),
            "source_line_end": int(source_line_end),
        }

    matched = _matching_live_section(
        project,
        section_title=section_title,
        section_index=section_index,
        source_file=normalized_file,
    )
    if matched is None:
        raise ValueError("matching source section not found; provide section_index or explicit source lines")
    return {
        "section_title": str(matched.get("title") or section_title).strip(),
        "section_index": int(matched["index"]) if matched.get("index") is not None else section_index,
        "source_file": _normalize_summary_source_file(project, str(matched.get("file_name") or normalized_file)),
        "source_line_start": int(matched["start_line"]),
        "source_line_end": int(matched["end_line"]),
    }


def refresh_section_summary_staleness(item: SectionSummary) -> SectionSummary:
    current_content = _read_text_line_range(item.project, item.source_file, item.source_line_start, item.source_line_end)
    current_hash = _hash_text(current_content)
    stale = current_hash != item.content_hash
    latest_version = _latest_version_number(item.project, item.source_file)
    if latest_version > item.source_version_number and current_hash != item.content_hash:
        stale = True
    dirty_fields: list[str] = []
    if item.is_stale != stale:
        item.is_stale = stale
        dirty_fields.append("is_stale")
    if dirty_fields:
        item.save(update_fields=[*dirty_fields, "updated_at"])
    return item


def serialize_section_summary(item: SectionSummary, *, refresh_staleness: bool = True) -> dict[str, Any]:
    if refresh_staleness:
        item = refresh_section_summary_staleness(item)
    return {
        "id": item.id,
        "section_title": item.section_title,
        "section_index": item.section_index,
        "source_file": item.source_file,
        "source_line_start": item.source_line_start,
        "source_line_end": item.source_line_end,
        "summary_text": item.summary_text,
        "written_by": item.written_by,
        "source_version_number": item.source_version_number,
        "current_version_number": _latest_version_number(item.project, item.source_file),
        "is_stale": item.is_stale,
        "updated_at": item.updated_at.isoformat(),
    }


def list_section_summaries(project, *, refresh_staleness: bool = True) -> list[dict[str, Any]]:
    return [
        serialize_section_summary(item, refresh_staleness=refresh_staleness)
        for item in SectionSummary.objects.filter(project=project).order_by("section_title")
    ]


def get_section_summary(project, *, section_title: str, refresh_staleness: bool = True) -> dict[str, Any]:
    item = SectionSummary.objects.filter(project=project, section_title=str(section_title).strip()).first()
    if item is None:
        raise ValueError("section summary not found")
    return serialize_section_summary(item, refresh_staleness=refresh_staleness)


def update_section_summary(
    project,
    *,
    section_title: str,
    summary_text: str,
    actor=None,
    source: str = "web",
    summary: str = "",
    section_index: int | None = None,
    source_file: str | None = None,
    source_line_start: int | None = None,
    source_line_end: int | None = None,
) -> dict[str, Any]:
    if not str(section_title or "").strip():
        raise ValueError("section_title is required")
    if not isinstance(summary_text, str):
        raise ValueError("summary_text must be a string")
    metadata = _resolve_summary_source_metadata(
        project,
        section_title=str(section_title).strip(),
        section_index=section_index,
        source_file=source_file,
        source_line_start=source_line_start,
        source_line_end=source_line_end,
    )
    section_content = _read_text_line_range(
        project,
        metadata["source_file"],
        metadata["source_line_start"],
        metadata["source_line_end"],
    )
    values = {
        **metadata,
        "content_hash": _hash_text(section_content),
        "summary_text": summary_text,
        "written_by": SectionSummary.WrittenBy.MCP if source == "mcp" else SectionSummary.WrittenBy.USER,
        "source_version_number": _latest_version_number(project, metadata["source_file"]),
        "is_stale": False,
    }
    item = SectionSummary.objects.filter(project=project, section_title=metadata["section_title"]).first()
    created = item is None
    before = serialize_model_instance(item) if item is not None else None
    if item is None:
        item = SectionSummary.objects.create(project=project, **values)
    else:
        for field_name, value in values.items():
            setattr(item, field_name, value)
        item.save()
    _audit_db_change(
        item,
        operation=AssistantAuditLog.Operation.CREATE if created else AssistantAuditLog.Operation.UPDATE,
        actor=actor,
        source=source,
        summary=summary or f"Updated section summary for {item.section_title}",
        before_snapshot=before,
    )
    return serialize_section_summary(item, refresh_staleness=False)


def mark_summaries_stale_for_version(version: ProjectVersion) -> int:
    target_file = str(version.target_file or "").strip()
    if not target_file:
        return 0
    updated = SectionSummary.objects.filter(
        project=version.project,
        source_file=target_file,
        source_version_number__lt=version.number,
        is_stale=False,
    ).update(is_stale=True)
    return int(updated)


def _match_outline_item(project, section_title: str) -> ProjectOutlineItem | None:
    return (
        ProjectOutlineItem.objects.filter(project=project, title=str(section_title).strip())
        .order_by("order", "id")
        .first()
    )


def _sync_requirement_section_refs(item: ProjectRequirement, section_refs: list[str]) -> None:
    normalized = []
    seen = set()
    for value in section_refs:
        title = str(value or "").strip()
        if title and title not in seen:
            normalized.append(title)
            seen.add(title)
    existing = {ref.section_title: ref for ref in item.section_refs.all()}
    keep_ids: list[int] = []
    for title in normalized:
        outline_item = _match_outline_item(item.project, title)
        ref = existing.get(title)
        if ref is None:
            ref = RequirementSectionRef.objects.create(
                requirement=item,
                section_title=title,
                outline_item=outline_item,
            )
        else:
            if ref.outline_item_id != (outline_item.id if outline_item else None):
                ref.outline_item = outline_item
                ref.save(update_fields=["outline_item"])
        keep_ids.append(ref.id)
    item.section_refs.exclude(id__in=keep_ids).delete()


def serialize_requirement(item: ProjectRequirement) -> dict[str, Any]:
    refs = list(item.section_refs.order_by("section_title"))
    return {
        "id": item.id,
        "req_id": item.req_id,
        "description": item.description,
        "coverage": item.coverage,
        "notes": item.notes,
        "updated_by": item.updated_by,
        "section_refs": [ref.section_title for ref in refs],
        "outline_item_ids": [ref.outline_item_id for ref in refs if ref.outline_item_id],
        "updated_at": item.updated_at.isoformat(),
    }


def list_requirements(project) -> list[dict[str, Any]]:
    return [serialize_requirement(item) for item in ProjectRequirement.objects.filter(project=project).order_by("req_id")]


def create_requirement(
    project,
    *,
    req_id: str,
    description: str,
    coverage: str = ProjectRequirement.Coverage.UNCHECKED,
    notes: str = "",
    section_refs: list[str] | None = None,
    actor=None,
    source: str = "web",
    summary: str = "",
) -> dict[str, Any]:
    if not str(req_id or "").strip():
        raise ValueError("req_id is required")
    if not str(description or "").strip():
        raise ValueError("description is required")
    item = ProjectRequirement.objects.create(
        project=project,
        req_id=str(req_id).strip(),
        description=str(description).strip(),
        coverage=coverage,
        notes=str(notes or ""),
        updated_by=ProjectRequirement.UpdatedBy.MCP if source == "mcp" else ProjectRequirement.UpdatedBy.USER,
    )
    _sync_requirement_section_refs(item, list(section_refs or []))
    _audit_db_change(
        item,
        operation=AssistantAuditLog.Operation.CREATE,
        actor=actor,
        source=source,
        summary=summary or f"Created requirement {item.req_id}",
    )
    return serialize_requirement(item)


def update_requirement(
    project,
    *,
    requirement_id: int,
    actor=None,
    source: str = "web",
    summary: str = "",
    **changes,
) -> dict[str, Any]:
    item = ProjectRequirement.objects.get(project=project, id=requirement_id)
    before = serialize_model_instance(item)
    section_refs = changes.pop("section_refs", None)
    if "req_id" in changes:
        item.req_id = str(changes.pop("req_id") or "").strip()
        if not item.req_id:
            raise ValueError("req_id is required")
    if "description" in changes:
        item.description = str(changes.pop("description") or "").strip()
        if not item.description:
            raise ValueError("description is required")
    if "coverage" in changes:
        item.coverage = str(changes.pop("coverage") or ProjectRequirement.Coverage.UNCHECKED)
    if "notes" in changes:
        item.notes = str(changes.pop("notes") or "")
    if changes:
        raise ValueError(f"unsupported requirement changes: {', '.join(sorted(changes))}")
    item.updated_by = ProjectRequirement.UpdatedBy.MCP if source == "mcp" else ProjectRequirement.UpdatedBy.USER
    item.save()
    if section_refs is not None:
        _sync_requirement_section_refs(item, list(section_refs))
    _audit_db_change(
        item,
        operation=AssistantAuditLog.Operation.UPDATE,
        actor=actor,
        source=source,
        summary=summary or f"Updated requirement {item.req_id}",
        before_snapshot=before,
    )
    return serialize_requirement(item)


def assert_longdoc_feature(project, feature_name: str, *, require_write: bool = False) -> ProjectLongDocSettings:
    settings_obj, _ = get_or_create_longdoc_settings(project)
    if feature_name == "enabled":
        enabled = settings_obj.enabled
    else:
        enabled = is_feature_enabled(settings_obj, feature_name)
    if not enabled:
        feature_label = feature_name.replace("_enabled", "").replace("_", " ")
        raise LongdocAccessError(
            error="FEATURE_DISABLED",
            message=f"{feature_label.title()} is disabled for this project.",
            status_code=409,
            suggestion="Enable long-document mode or turn on this Writing Assistant feature in project settings.",
            extra={"feature": feature_name},
        )
    if require_write:
        try:
            assert_not_locked(project)
        except ProjectLockedError as exc:
            raise LongdocAccessError(
                error="PROJECT_LOCKED",
                message=str(exc),
                status_code=423,
                suggestion="Wait for the active AI session to finish or unlock the project before editing Writing Assistant data.",
                extra={"session_id": exc.session.id},
            ) from exc
    return settings_obj


_LONGDOC_FIELD_SYSTEM_DEFAULTS: dict[str, bool] = {
    "enabled": True,
    "context_enabled": True,
    "outline_enabled": True,
    "tasks_enabled": True,
    "notes_enabled": True,
    "summaries_enabled": True,
    "requirements_enabled": False,
    "ai_sessions_enabled": True,
}

# Mapping: ProjectLongDocSettings field → Template.longdoc_* field
_TEMPLATE_LONGDOC_FIELDS = {
    "enabled": "longdoc_enabled",
    "context_enabled": "longdoc_context_enabled",
    "outline_enabled": "longdoc_outline_enabled",
    "tasks_enabled": "longdoc_tasks_enabled",
    "notes_enabled": "longdoc_notes_enabled",
    "summaries_enabled": "longdoc_summaries_enabled",
    "requirements_enabled": "longdoc_requirements_enabled",
    "ai_sessions_enabled": "longdoc_ai_sessions_enabled",
}


def _has_longdoc_template_data(template) -> bool:
    return (
        any(getattr(template, tfield, None) is not None for tfield in _TEMPLATE_LONGDOC_FIELDS.values())
        or template.outline_items.exists()
        or template.tasks.exists()
        or template.note_sections.exists()
        or template.context_files.exists()
    )


def initialize_longdoc_from_template(project, template) -> ProjectLongDocSettings | None:
    """
    Initialize long-document data on a newly-created project from a template.

    Uses the template's longdoc_* fields for ProjectLongDocSettings (null = system default).
    Copies outline items, requirements, tasks, note sections, and DB context files.
    Also extracts .smarttex/context/ files from the template ZIP as additional context files.

    Returns None (no side effects) when the template has no longdoc data at all.
    """
    from templates_lib.services import extract_smarttex_context_from_zip

    has_zip_context = bool(template.zip_file)  # checked lazily below

    if not _has_longdoc_template_data(template) and not has_zip_context:
        return None

    # Build settings config: system defaults overridden by template fields
    config = dict(_LONGDOC_FIELD_SYSTEM_DEFAULTS)
    for proj_field, tmpl_field in _TEMPLATE_LONGDOC_FIELDS.items():
        val = getattr(template, tmpl_field, None)
        if val is not None:
            config[proj_field] = val

    settings_obj = ProjectLongDocSettings.objects.create(project=project, **config)

    outline_rows = list(template.outline_items.order_by("order"))
    if outline_rows:
        ProjectOutlineItem.objects.bulk_create([
            ProjectOutlineItem(
                project=project,
                order=item.order,
                title=item.title,
                level=max(1, int(item.level)),
                status=item.status,
                expected_pages=item.expected_pages,
                notes=item.notes,
            )
            for item in outline_rows
        ])

    requirement_rows = list(template.requirements.order_by("req_id"))
    if requirement_rows:
        ProjectRequirement.objects.bulk_create([
            ProjectRequirement(
                project=project,
                req_id=item.req_id,
                description=item.description,
            )
            for item in requirement_rows
        ])

    task_rows = list(template.tasks.order_by("id"))
    if task_rows:
        ProjectTask.objects.bulk_create([
            ProjectTask(
                project=project,
                description=item.description,
                created_by=ProjectTask.CreatedBy.USER,
            )
            for item in task_rows
        ])

    note_rows = list(template.note_sections.order_by("order"))
    if note_rows:
        ProjectNoteSection.objects.bulk_create([
            ProjectNoteSection(
                project=project,
                heading=item.heading,
                body=item.body,
                order=item.order,
            )
            for item in note_rows
        ])
    else:
        ensure_default_note_sections(project)

    context_root = ensure_context_dir(project)

    context_rows = list(template.context_files.order_by("filename"))
    for item in context_rows:
        try:
            rel = _safe_context_rel_path(item.filename)
        except ValueError:
            continue
        target = (context_root / rel).resolve()
        if context_root.resolve() not in target.parents and context_root.resolve() != target.parent:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_text(item.content, encoding="utf-8")
        except OSError:
            continue
        ProjectContextFile.objects.get_or_create(
            project=project,
            filename=str(rel).replace("\\", "/"),
            defaults={
                "display_name": item.display_name.strip() or _context_display_name(item.filename),
                "description": item.description.strip(),
                "is_read_only": False,
                "size_bytes": target.stat().st_size,
            },
        )

    # Also extract .smarttex/context/ files from the template ZIP (does not overwrite DB entries)
    for entry in extract_smarttex_context_from_zip(template, context_root):
        filename = entry["filename"]
        ProjectContextFile.objects.get_or_create(
            project=project,
            filename=filename,
            defaults={
                "display_name": _context_display_name(filename),
                "description": "",
                "is_read_only": False,
                "size_bytes": entry["size"],
            },
        )

    return settings_obj


def overview_payload(project) -> dict[str, Any]:
    from .locks import get_locking_change_proposal, get_locking_session
    from .proposal_service import serialize_change_proposal
    settings_obj, _ = get_or_create_longdoc_settings(project)
    sync_context_file_records(project)
    locking_session = get_locking_session(project)
    locking_proposal = get_locking_change_proposal(project)
    context_items = list(ProjectContextFile.objects.filter(project=project).order_by("filename"))
    outline_items = list(ProjectOutlineItem.objects.filter(project=project).order_by("order", "id"))
    tasks = list(ProjectTask.objects.filter(project=project).order_by("status", "-updated_at"))
    notes = list(ProjectNoteSection.objects.filter(project=project).order_by("order", "id"))
    summaries = list(SectionSummary.objects.filter(project=project).order_by("section_title"))
    requirements = list(ProjectRequirement.objects.filter(project=project).order_by("req_id"))
    stale_summary_count = 0
    for item in summaries:
        refresh_section_summary_staleness(item)
        if item.is_stale:
            stale_summary_count += 1
    return {
        "project_id": project.id,
        "settings": serialize_settings(settings_obj),
        "context_files": [
            {
                "filename": item.filename,
                "display_name": item.display_name,
                "description": item.description,
                "size_bytes": item.size_bytes,
            }
            for item in context_items[:10]
        ],
        "context_file_count": len(context_items),
        "outline_items": [
            {
                "id": item.id,
                "order": item.order,
                "title": item.title,
                "level": item.level,
                "status": item.status,
            }
            for item in outline_items[:12]
        ],
        "outline_item_count": len(outline_items),
        "task_counts": {
            "open": sum(1 for item in tasks if item.status == ProjectTask.Status.OPEN),
            "in_progress": sum(1 for item in tasks if item.status == ProjectTask.Status.IN_PROGRESS),
            "done": sum(1 for item in tasks if item.status == ProjectTask.Status.DONE),
        },
        "tasks": [
            {
                "id": item.id,
                "description": item.description,
                "status": item.status,
            }
            for item in tasks[:12]
        ],
        "note_sections": [
            {
                "id": item.id,
                "heading": item.heading,
                "preview": (item.body.strip().splitlines()[0][:160] if item.body.strip() else ""),
            }
            for item in notes[:12]
        ],
        "note_section_count": len(notes),
        "summary_count": len(summaries),
        "stale_summary_count": stale_summary_count,
        "requirement_count": len(requirements),
        "requirement_coverage_counts": {
            "unchecked": sum(1 for item in requirements if item.coverage == ProjectRequirement.Coverage.UNCHECKED),
            "covered": sum(1 for item in requirements if item.coverage == ProjectRequirement.Coverage.COVERED),
            "partial": sum(1 for item in requirements if item.coverage == ProjectRequirement.Coverage.PARTIAL),
            "missing": sum(1 for item in requirements if item.coverage == ProjectRequirement.Coverage.MISSING),
        },
        "active_session": (
            {
                "id": locking_session.id,
                "goal": locking_session.goal,
                "status": locking_session.status,
                "expires_at": locking_session.expires_at.isoformat(),
            }
            if locking_session is not None and locking_proposal is None
            else None
        ),
        "active_proposal": serialize_change_proposal(locking_proposal),
        "writing_workflow_guidance": (
            "Before proposing a large text change, check the outline for missing or stub sections, "
            "use line-targeted reads to locate the edit area, and inspect the document graph before "
            "creating new source files."
        ),
    }
