"""Freshness predicates for the navigation index.

Phase 2: index- and card-level freshness. Partial-refresh signals (Phase
5) are not implemented here; we only evaluate state of already-built
index rows.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from django.utils import timezone

from projects.models import Project
from projects.services import main_source_filename, project_dir

from ..models import (
    FileCard,
    IndexStatus,
    NAV_SCHEMA_VERSION,
    ProjectNavigationIndex,
    RegionCard,
)


@dataclass
class IndexFreshness:
    status: str  # current | partial_stale | whole_invalid | absent | failed | building
    reasons: list[str]


def evaluate_index(project: Project) -> IndexFreshness:
    try:
        index = ProjectNavigationIndex.objects.get(project=project)
    except ProjectNavigationIndex.DoesNotExist:
        return IndexFreshness(status="absent", reasons=["index_absent"])

    if index.status == IndexStatus.BUILDING:
        return IndexFreshness(status="building", reasons=["index_building"])
    if index.status == IndexStatus.FAILED:
        return IndexFreshness(status="failed", reasons=["last_build_failed"])
    if index.status in (IndexStatus.PENDING,) and not index.last_built_at:
        return IndexFreshness(status="absent", reasons=["never_built"])

    reasons: list[str] = []
    if index.schema_version < NAV_SCHEMA_VERSION:
        reasons.append("schema_version_outdated")
    if (project.markup_type or "") != (index.markup_type_snapshot or ""):
        reasons.append("markup_type_changed")
    expected_main = main_source_filename(project)
    if expected_main != (index.main_file_snapshot or ""):
        reasons.append("main_file_changed")
    if reasons:
        return IndexFreshness(status="whole_invalid", reasons=reasons)

    stale_count = index.file_cards.filter(is_stale=True).count()
    total = index.file_cards.count()
    if total and stale_count / max(1, total) > 0.25:
        return IndexFreshness(status="partial_stale", reasons=["many_stale_cards"])

    return IndexFreshness(status="current", reasons=[])


def file_card_is_fresh(card: FileCard, project: Project) -> bool:
    if card.is_stale:
        return False
    path = project_dir(project) / card.filename
    if not path.exists():
        return False
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return hashlib.sha256(data).hexdigest() == (card.content_hash or "")


def region_card_is_fresh(region: RegionCard, project: Project) -> bool:
    if region.is_stale:
        return False
    file_card = region.file_card
    path = project_dir(project) / file_card.filename
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    lines = text.splitlines()
    slice_text = "\n".join(
        lines[max(0, region.line_start - 1): max(0, region.line_end)]
    )
    return (
        hashlib.sha256(slice_text.encode("utf-8", errors="replace")).hexdigest()
        == (region.content_hash or "")
    )


def preparation_is_fresh(
    payload: dict, *, project: Project, index: Optional[ProjectNavigationIndex]
) -> bool:
    """Cheap reuse check: schema/version anchor + reuse_count + TTL (TTL is
    enforced by the cache backend itself)."""
    if not payload or not index:
        return False
    if int(payload.get("schema_version", 0)) != int(index.schema_version):
        return False
    if int(payload.get("base_version_number", -1)) != int(index.last_built_version_number):
        return False
    if (payload.get("markup_type_snapshot") or "") != (index.markup_type_snapshot or ""):
        return False
    if (payload.get("main_file_snapshot") or "") != (index.main_file_snapshot or ""):
        return False
    return True
