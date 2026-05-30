"""Thin MCP wrapper around :func:`navigation.services.preparation.prepare_document_work`.

Imported and registered by ``mcp_http_server.py``. Keeps the MCP module
free of navigation-internal details.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from projects.models import Project

from .services.preparation import prepare_document_work as _prepare_document_work

logger = logging.getLogger(__name__)


PREPARE_DOCUMENT_WORK_DOCSTRING = """\
Prepare a document/project write workflow.

Call this BEFORE any document/project write workflow: any proposal, any
edit, any multi-file change, any retry after validation failure, any
operation touching structure / sections / templates / requirements /
consistency / cleanup / long-document content.

Skip the call only when continuing the same workflow with a still-fresh
``preparation_id`` from the previous call, or when the user is only
asking for explanation/analysis (no write tool will be used).

After a validation or compile failure, call this tool again with
``previous_error=<error response>`` and ``attempted_patch_ops=<ops you
tried>`` — the response will include ``repair_guidance``.

Use the returned ``read_targets``, ``likely_edit_targets``,
``constraints``, ``patch_op_schema_reminder``, and ``do_not`` to plan
your reads and edits. Do not check settings or feature flags first —
feature availability is reported inside ``capabilities``.

This tool never edits source files, never creates or modifies proposals,
and never takes proposal locks. It MAY build/refresh navigation index
rows and write a short-lived preparation cache entry.
"""


def prepare_document_work_tool(
    project_id: int,
    user_request: str,
    preparation_id: Optional[str] = None,
    previous_error: Optional[dict] = None,
    attempted_patch_ops: Optional[list[dict]] = None,
    selected_file: Optional[str] = None,
    selected_region_id: Optional[int] = None,
) -> dict[str, Any]:
    try:
        project = Project.objects.get(pk=int(project_id))
    except (Project.DoesNotExist, ValueError, TypeError):
        return {
            "error": "PROJECT_NOT_FOUND",
            "message": f"No project with id {project_id!r}.",
            "suggestion": "Call list_projects to find a valid project id.",
        }
    try:
        return _prepare_document_work(
            project,
            user_request=user_request or "",
            preparation_id=preparation_id,
            previous_error=previous_error,
            attempted_patch_ops=attempted_patch_ops,
            selected_file=selected_file,
            selected_region_id=selected_region_id,
        )
    except Exception as exc:  # pragma: no cover - last-resort guard
        logger.exception("prepare_document_work tool failed: %s", exc)
        return {
            "error": "INTERNAL_ERROR",
            "message": f"prepare_document_work failed: {type(exc).__name__}",
            "suggestion": "Retry once; if the error persists, fall back to existing read tools.",
        }
