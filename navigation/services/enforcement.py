"""Preparation-enforcement helpers for the navigation layer.

Called by Django views to answer: is there a fresh preparation for this
project, and what enforcement mode applies?
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from projects.models import Project


def get_enforcement_mode(project: "Project") -> str:
    try:
        settings = project.longdoc_settings
        return settings.preparation_enforcement_mode or "off"
    except Exception:
        return "off"


def check_preparation_freshness(project: "Project", preparation_id: str | None) -> dict:
    """Return freshness info for a preparation_id against the current project index."""
    from . import cache as prep_cache
    from . import freshness as fr
    from ..models import ProjectNavigationIndex

    if not preparation_id:
        return {"fresh": False, "reason": "no_preparation_id", "mode": "none"}

    cached = prep_cache.lookup_by_id(preparation_id)
    if not cached:
        return {"fresh": False, "reason": "not_found_or_expired", "mode": "none"}

    if not prep_cache.is_reusable(cached):
        return {"fresh": False, "reason": "max_reuse_exceeded", "mode": cached.get("mode", "")}

    try:
        index = ProjectNavigationIndex.objects.get(project=project)
    except ProjectNavigationIndex.DoesNotExist:
        return {"fresh": False, "reason": "no_index", "mode": cached.get("mode", "")}

    if not fr.preparation_is_fresh(cached, project=project, index=index):
        return {"fresh": False, "reason": "stale_index", "mode": cached.get("mode", "")}

    return {
        "fresh": True,
        "reason": "",
        "mode": cached.get("mode", ""),
        "reuse_count": int(cached.get("reuse_count", 0)),
        "context_bundle_present": bool(cached.get("context_bundle")),
    }
