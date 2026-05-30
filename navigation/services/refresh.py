"""Partial refresh for the navigation index.

A partial refresh recomputes hashes for the touched files only, updates
the corresponding ``FileCard``/``RegionCard`` rows, and (if any touched
file is a source file) re-derives the include/reachability snapshot.

It NEVER drops the whole index. If the touched-file set is empty, it
behaves as a freshness sweep that only marks already-rotted cards stale.
Authoritative file content always lives on disk; cards are bookkeeping.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from django.db import transaction
from django.utils import timezone

from projects.models import Project, ProjectVersion
from projects.services import main_source_filename, project_dir

from longdoc.document_graph import inspect_document_graph

from ..models import (
    Confidence,
    FileCard,
    FileRole,
    IndexStatus,
    ProjectNavigationIndex,
    Reachability,
    RegionCard,
    Source,
)
from .discovery import classify_file_role, discover_project_files
from .state_heuristics import classify_state
from .structure import (
    deterministic_file_triggers,
    deterministic_region_triggers,
    extract_regions,
)
from . import cache as prep_cache

logger = logging.getLogger(__name__)


@dataclass
class RefreshSummary:
    project_id: int
    status: str = "refreshed"
    touched_files: list[str] = field(default_factory=list)
    files_updated: int = 0
    files_marked_missing: int = 0
    regions_updated: int = 0
    error: str = ""


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def refresh_navigation_index(
    project: Project,
    *,
    files: Optional[Iterable[str]] = None,
) -> RefreshSummary:
    """Refresh only the listed files (or all stale cards if ``files`` is empty).

    On any non-recoverable error the call returns ``status='failed'``
    and never raises, so callers (signals, MCP tools) can degrade
    gracefully.
    """
    summary = RefreshSummary(project_id=project.id)
    try:
        index = ProjectNavigationIndex.objects.filter(project=project).first()
    except Exception as exc:  # pragma: no cover - defensive
        summary.status = "failed"
        summary.error = str(exc)[:500]
        return summary
    if index is None:
        summary.status = "no_index"
        return summary

    target_names: set[str] = {f for f in (files or []) if f}
    if not target_names:
        target_names = set(
            index.file_cards.filter(is_stale=True).values_list("filename", flat=True)
        )

    summary.touched_files = sorted(target_names)
    if not target_names:
        summary.status = "noop"
        return summary

    root = project_dir(project)
    main_file = main_source_filename(project)
    version_number = _latest_version_number(project)

    source_touched = any(
        Path(name).suffix.lower() in {".tex", ".typ"} for name in target_names
    )

    # Recompute include graph only when we touched something that could
    # influence reachability. Keep this off the fast path otherwise.
    reachable_set: set[str] = set()
    if source_touched:
        try:
            graph = inspect_document_graph(project)
            reachable_set = set(graph.reachable_files) if graph else set()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("partial refresh graph inspect failed: %s", exc)

    for filename in target_names:
        try:
            card = index.file_cards.filter(filename=filename).first()
            path = root / filename
            if not path.exists():
                if card is not None:
                    card.reachability = Reachability.MISSING
                    card.is_stale = True
                    if not card.exclusion_reason:
                        card.exclusion_reason = "file_deleted"
                    card.save(update_fields=[
                        "reachability", "is_stale", "exclusion_reason", "updated_at"
                    ])
                summary.files_marked_missing += 1
                continue

            try:
                raw = path.read_bytes()
            except OSError:
                continue
            content_hash = _hash_bytes(raw)
            try:
                text = raw.decode("utf-8", errors="ignore")
            except Exception:
                text = ""
            line_count = text.count("\n") + (0 if text.endswith("\n") else 1) if text else 0
            byte_size = len(raw)

            is_entrypoint = filename == main_file
            role, role_conf = classify_file_role(filename, is_entrypoint=is_entrypoint)
            state, state_conf = classify_state(text)

            region_infos = extract_regions(
                filename=filename,
                content=text,
                markup_type=project.markup_type,
                role=role,
            )
            region_titles = [info.title for info in region_infos if info.title]
            file_triggers = deterministic_file_triggers(
                filename=filename, role=role, region_titles=region_titles
            )
            file_summary = _file_summary(role=role, line_count=line_count, byte_size=byte_size, region_count=len(region_infos))

            if card is None:
                # First time we see this file — create the card.
                card = FileCard.objects.create(
                    index=index,
                    filename=filename,
                    role=role,
                    role_source=Source.DETERMINISTIC,
                    role_confidence=role_conf,
                    state=state,
                    state_source=Source.DETERMINISTIC,
                    state_confidence=state_conf,
                    reachability=(
                        Reachability.REACHABLE
                        if (is_entrypoint or filename in reachable_set)
                        else Reachability.ORPHAN
                    ),
                    summary=file_summary,
                    summary_source=Source.DETERMINISTIC,
                    summary_confidence=Confidence.LOW,
                    edit_triggers=file_triggers,
                    triggers_source=Source.DETERMINISTIC,
                    line_count=line_count,
                    byte_size=byte_size,
                    content_hash=content_hash,
                    last_version_number=version_number,
                    last_indexed_at=timezone.now(),
                    is_stale=False,
                    exclusion_reason="",
                )
            else:
                with transaction.atomic():
                    card.role = role
                    card.role_source = Source.DETERMINISTIC
                    card.role_confidence = role_conf
                    # Preserve small-model state overrides only when current
                    # state was small-model authored AND confidence isn't
                    # explicitly demoted; otherwise refresh deterministically.
                    if card.state_source != Source.SMALL_MODEL:
                        card.state = state
                        card.state_source = Source.DETERMINISTIC
                        card.state_confidence = state_conf
                    if source_touched:
                        if is_entrypoint or filename in reachable_set:
                            card.reachability = Reachability.REACHABLE
                        elif card.reachability != Reachability.MISSING:
                            card.reachability = Reachability.ORPHAN
                    if card.summary_source != Source.SMALL_MODEL:
                        card.summary = file_summary
                        card.summary_source = Source.DETERMINISTIC
                        card.summary_confidence = Confidence.LOW
                    if card.triggers_source != Source.SMALL_MODEL:
                        card.edit_triggers = file_triggers
                        card.triggers_source = Source.DETERMINISTIC
                    card.line_count = line_count
                    card.byte_size = byte_size
                    card.content_hash = content_hash
                    card.last_version_number = version_number
                    card.last_indexed_at = timezone.now()
                    card.is_stale = False
                    card.exclusion_reason = ""
                    card.save()

            # Region cards — re-extract for this file.
            existing = {r.order: r for r in card.region_cards.all()}
            seen_orders: set[int] = set()
            for info in region_infos:
                slice_text = "\n".join(
                    text.splitlines()[max(0, info.line_start - 1): max(0, info.line_end)]
                )
                content_hash_region = hashlib.sha256(
                    slice_text.encode("utf-8", errors="replace")
                ).hexdigest()
                region_state, region_state_conf = classify_state(slice_text)
                triggers = deterministic_region_triggers(title=info.title)
                region_summary = (info.title or info.region_kind or "")[:280]
                region = existing.get(info.order)
                if region is None:
                    RegionCard.objects.create(
                        file_card=card,
                        order=info.order,
                        region_kind=info.region_kind,
                        title=info.title[:512],
                        level=info.level,
                        line_start=info.line_start,
                        line_end=info.line_end,
                        state=region_state,
                        state_source=Source.DETERMINISTIC,
                        state_confidence=region_state_conf,
                        summary=region_summary,
                        summary_source=Source.DETERMINISTIC,
                        summary_confidence=Confidence.LOW,
                        edit_triggers=triggers,
                        triggers_source=Source.DETERMINISTIC,
                        content_hash=content_hash_region,
                        last_indexed_at=timezone.now(),
                        is_stale=False,
                    )
                else:
                    region.region_kind = info.region_kind
                    region.title = info.title[:512]
                    region.level = info.level
                    region.line_start = info.line_start
                    region.line_end = info.line_end
                    if region.state_source != Source.SMALL_MODEL:
                        region.state = region_state
                        region.state_source = Source.DETERMINISTIC
                        region.state_confidence = region_state_conf
                    if region.summary_source != Source.SMALL_MODEL:
                        region.summary = region_summary
                        region.summary_source = Source.DETERMINISTIC
                        region.summary_confidence = Confidence.LOW
                    if region.triggers_source != Source.SMALL_MODEL:
                        region.edit_triggers = triggers
                        region.triggers_source = Source.DETERMINISTIC
                    region.content_hash = content_hash_region
                    region.last_indexed_at = timezone.now()
                    region.is_stale = False
                    region.save()
                seen_orders.add(info.order)
                summary.regions_updated += 1

            for order_key, region in existing.items():
                if order_key not in seen_orders:
                    region.is_stale = True
                    region.save(update_fields=["is_stale", "updated_at"])

            summary.files_updated += 1

        except Exception as exc:  # pragma: no cover - per-file isolation
            logger.exception(
                "partial refresh failed for %s in project %s: %s",
                filename,
                project.id,
                exc,
            )

    # Persist coverage snapshot timestamps.
    try:
        index.last_partial_refresh_at = timezone.now()
        index.last_built_version_number = version_number
        index.save(update_fields=[
            "last_partial_refresh_at",
            "last_built_version_number",
            "updated_at",
        ])
    except Exception:  # pragma: no cover - defensive
        pass

    # Invalidate preparation cache hint: bumping ``last_built_version_number``
    # alone is enough for the request-keyed cache to miss; ``invalidate_project``
    # is a no-op for portable backends but kept as a contract surface.
    try:
        prep_cache.invalidate_project(project.id)
    except Exception:  # pragma: no cover
        pass

    return summary


def mark_files_stale(project: Project, filenames: Iterable[str]) -> int:
    """Mark cards stale without re-reading content. Returns count."""
    names = [f for f in (filenames or []) if f]
    if not names:
        return 0
    index = ProjectNavigationIndex.objects.filter(project=project).first()
    if index is None:
        return 0
    qs = index.file_cards.filter(filename__in=names)
    updated = qs.update(is_stale=True, updated_at=timezone.now())
    RegionCard.objects.filter(file_card__in=qs).update(
        is_stale=True, updated_at=timezone.now()
    )
    return int(updated or 0)


def mark_whole_index_stale(project: Project, *, reason: str = "") -> None:
    """Mark the index ``whole_invalid``-ish: every card stale + index PENDING."""
    index = ProjectNavigationIndex.objects.filter(project=project).first()
    if index is None:
        return
    index.file_cards.update(is_stale=True, updated_at=timezone.now())
    RegionCard.objects.filter(file_card__index=index).update(
        is_stale=True, updated_at=timezone.now()
    )
    if reason:
        index.build_error = reason[:2000]
    index.status = IndexStatus.PENDING
    index.save(update_fields=["build_error", "status", "updated_at"])


def _file_summary(*, role: str, line_count: int, byte_size: int, region_count: int) -> str:
    parts = [str(role)]
    if region_count:
        parts.append(f"{region_count} region(s)")
    parts.append(f"{line_count} lines")
    parts.append(f"{byte_size} bytes")
    return "; ".join(parts)[:280]


def _latest_version_number(project: Project) -> int:
    latest = (
        ProjectVersion.objects.filter(project=project)
        .order_by("-number")
        .values_list("number", flat=True)
        .first()
    )
    return int(latest or 0)
