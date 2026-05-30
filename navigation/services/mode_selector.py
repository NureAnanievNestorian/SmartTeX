"""Deterministic mode selection for ``prepare_document_work``.

Phase 2 + 3 modes: ``repair``, ``cheap_direct``, ``indexed_keyword``,
``fallback_structural``, ``minimal``. (``indexed_reranked`` belongs to
Phase 7 and is not selected here.)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ..models import IndexStatus, ProjectNavigationIndex, FileCard, RegionCard

from small_model.services.pre_proposal import (
    _BROAD_SCOPE_RE,
    _REPLACE_VERBS_RE,
)


@dataclass
class SelectionInputs:
    user_request: str
    preparation_id: Optional[str]
    previous_error: Optional[dict]
    attempted_patch_ops: Optional[list[dict]]
    selected_file: Optional[str]
    selected_region_id: Optional[int]


def _narrow_verb_match(text: str) -> bool:
    if not text:
        return False
    if _REPLACE_VERBS_RE.search(text) and not _BROAD_SCOPE_RE.search(text):
        return True
    return False


def cheap_direct_match(
    inputs: SelectionInputs, index: Optional[ProjectNavigationIndex]
) -> Optional[dict]:
    """Return a ``{'file_card': ..., 'region_card': ...}`` hit, or None."""
    if not index:
        return None
    text = inputs.user_request or ""

    # Trigger 1: explicit selection.
    if inputs.selected_file and inputs.selected_region_id is not None:
        try:
            file_card = index.file_cards.get(filename=inputs.selected_file)
        except FileCard.DoesNotExist:
            file_card = None
        region_card = None
        if file_card:
            try:
                region_card = file_card.region_cards.get(pk=inputs.selected_region_id)
            except RegionCard.DoesNotExist:
                region_card = None
        if file_card and region_card and _narrow_verb_match(text):
            return {"file_card": file_card, "region_card": region_card, "trigger": "explicit_selection"}

    # Trigger 3: exact unique filename or region title mention.
    if _narrow_verb_match(text):
        lowered = text.lower()
        file_hits = [
            fc for fc in index.file_cards.all()
            if fc.filename and fc.filename.lower() in lowered
        ]
        if len(file_hits) == 1:
            return {
                "file_card": file_hits[0],
                "region_card": None,
                "trigger": "exact_filename_mention",
            }

        region_hits: list[RegionCard] = []
        for rc in RegionCard.objects.filter(file_card__index=index).select_related("file_card"):
            title = (rc.title or "").strip().lower()
            if not title or len(title) < 4:
                continue
            if title in lowered:
                region_hits.append(rc)
        if len(region_hits) == 1:
            return {
                "file_card": region_hits[0].file_card,
                "region_card": region_hits[0],
                "trigger": "exact_region_mention",
            }

    return None


def select_mode(
    inputs: SelectionInputs,
    *,
    index: Optional[ProjectNavigationIndex],
    index_freshness_status: str,
    graph_available: bool,
) -> str:
    if inputs.previous_error or inputs.attempted_patch_ops:
        return "repair"

    cheap_hit = cheap_direct_match(inputs, index)
    if cheap_hit is not None and index_freshness_status in {"current", "partial_stale"}:
        return "cheap_direct"

    if index is None or index_freshness_status in {"absent", "failed", "building"}:
        return "fallback_structural" if graph_available else "minimal"

    if index_freshness_status == "whole_invalid":
        return "fallback_structural" if graph_available else "minimal"

    return "indexed_keyword"
