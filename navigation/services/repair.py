"""Deterministic repair-mode guidance for ``prepare_document_work``.

Maps known error codes / shapes returned by other tools (proposal
validation, session writes, document graph) to a ``repair_guidance``
payload that the main model can act on directly.
"""
from __future__ import annotations

from typing import Any, Optional


# Public op vocabulary, kept identical to ``longdoc.proposal_service.PROPOSAL_PATCH_OPS``.
ALLOWED_OPS = [
    "create_new_file",
    "patch_file_lines",
    "replace_text",
    "append_to_file",
    "update_section",
    "include_file",
]

COMMON_OP_MISTAKES = [
    "patch_lines (use patch_file_lines)",
    "replace_file (use patch_file_lines with explicit line range, or update_section)",
    "rewrite_section (use update_section)",
    "full_file_overwrite (use update_section or multiple patch_file_lines)",
    "delete_file / rename_file (not allowed as proposal ops)",
    "read_project_file / update_project_file (these are tools, not ops)",
]

INCLUDE_FILE_REQUIRES = [
    "filename",
    "include_target",
    "exactly one of anchor_after / anchor_before",
]

_OP_ALIASES = {
    "patch_lines": "patch_file_lines",
    "replace_file": "patch_file_lines",
    "rewrite_section": "update_section",
    "full_file_overwrite": "update_section",
    "create_file": "create_new_file",
    "new_file": "create_new_file",
    "insert_after_anchor": "patch_file_lines",
    "insert_before_anchor": "patch_file_lines",
    "replace_between_anchors": "patch_file_lines",
}


def patch_op_schema_reminder() -> dict[str, Any]:
    return {
        "allowed_ops": list(ALLOWED_OPS),
        "common_mistakes": list(COMMON_OP_MISTAKES),
        "include_file_requires": list(INCLUDE_FILE_REQUIRES),
    }


def _error_code(previous_error: Optional[dict]) -> str:
    if not previous_error:
        return ""
    for key in ("error", "code", "error_code", "kind"):
        val = previous_error.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _did_you_mean(previous_error: dict) -> str:
    val = previous_error.get("did_you_mean")
    if isinstance(val, str):
        return val
    details = previous_error.get("details")
    if isinstance(details, dict):
        v = details.get("did_you_mean")
        if isinstance(v, str):
            return v
    return ""


def _bad_op_from_attempts(attempted_patch_ops: Optional[list[dict]]) -> str:
    if not attempted_patch_ops:
        return ""
    for op in attempted_patch_ops:
        if not isinstance(op, dict):
            continue
        name = str(op.get("op") or "").strip()
        if name and name not in ALLOWED_OPS:
            return name
    return ""


def _read_targets_from_error(previous_error: Optional[dict]) -> list[dict[str, Any]]:
    """Best-effort: surface the filename/line in the error as a read target."""
    if not previous_error:
        return []
    targets: list[dict[str, Any]] = []
    filename = (
        previous_error.get("file")
        or previous_error.get("filename")
        or (previous_error.get("details") or {}).get("filename")
    )
    line = (
        previous_error.get("line")
        or (previous_error.get("details") or {}).get("line")
    )
    if filename:
        start = int(line) - 5 if isinstance(line, int) else 1
        end = int(line) + 5 if isinstance(line, int) else 80
        targets.append({
            "filename": str(filename),
            "line_start": max(1, start),
            "line_end": max(start + 1, end),
            "region_card_id": None,
            "kind": "file",
            "reason": "Read the area referenced by the previous error before retrying.",
            "confidence": "medium",
            "suggested_tool": "read_file_lines",
        })
    return targets


def build_repair_guidance(
    *,
    previous_error: Optional[dict],
    attempted_patch_ops: Optional[list[dict]],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Return ``(repair_guidance, read_targets, warnings)``.

    ``repair_guidance`` is always a dict (never None) when this function is
    called — the caller has already decided we're in repair mode.
    """
    warnings: list[str] = []
    code = _error_code(previous_error)
    bad_op = _bad_op_from_attempts(attempted_patch_ops)
    suggested = _did_you_mean(previous_error or {})
    if not suggested and bad_op:
        suggested = _OP_ALIASES.get(bad_op, "")

    error_kind = "other"
    diagnosis = "Could not classify the previous failure; re-read relevant files before retrying."
    fix_hint: dict[str, Any] = {
        "rewrite_op": None,
        "add_op": None,
        "additional_read_targets": [],
    }

    if code == "UNKNOWN_OP" or (bad_op and bad_op not in ALLOWED_OPS):
        error_kind = "unknown_op"
        target = suggested or _OP_ALIASES.get(bad_op or "", "")
        if target:
            diagnosis = (
                f"`{bad_op or 'the attempted op'}` is not a valid proposal op; "
                f"use `{target}` instead."
            )
            fix_hint["rewrite_op"] = {"from": bad_op or "unknown", "to": target}
        else:
            diagnosis = (
                f"`{bad_op or 'the attempted op'}` is not in allowed_ops. "
                f"Use one of: {', '.join(ALLOWED_OPS)}."
            )

    elif code in {"SOURCE_FILE_NOT_INCLUDED", "MISSING_INCLUDE", "ORPHAN_NEW_FILE"}:
        error_kind = "include_required"
        diagnosis = (
            "A new source file was created but never wired into the document "
            "graph. Add an `include_file` op (or a `patch_file_lines` op that "
            "inserts the include/input directive) in the same proposal."
        )
        fix_hint["add_op"] = {
            "op": "include_file",
            "required_fields": list(INCLUDE_FILE_REQUIRES),
        }

    elif code in {"USE_PROPOSAL_WORKFLOW"}:
        error_kind = "use_proposal"
        diagnosis = (
            "Direct writes to source files are rejected in controlled MCP "
            "mode. Re-issue the change via `propose_document_change` with "
            "the appropriate patch ops."
        )

    elif code in {"STALE_VALIDATION_TOKEN", "INVALID_VALIDATION_TOKEN"}:
        error_kind = "stale_token"
        diagnosis = (
            "The validation token is no longer valid (project HEAD moved or "
            "the token expired). Re-validate before proposing again."
        )

    elif code in {"OUT_OF_RANGE", "LINE_OUT_OF_RANGE", "INVALID_LINE_RANGE"} or (
        isinstance(previous_error, dict)
        and "out of range" in str(previous_error.get("message", "")).lower()
    ):
        error_kind = "out_of_bounds"
        diagnosis = (
            "The requested line range is outside the file. Re-read the file "
            "with `file_line_count` and `read_file_lines` before retrying."
        )

    elif code in {"GRAPH_ERROR", "DOCUMENT_GRAPH_ERROR"}:
        error_kind = "graph_error"
        diagnosis = (
            "The document graph rejected the change (likely a broken include "
            "edge). Re-read the entrypoint and any newly introduced include "
            "directives before retrying."
        )

    elif not previous_error and attempted_patch_ops:
        diagnosis = (
            "No structured error was provided. Re-read the targets you "
            "intended to edit and re-validate before retrying."
        )

    read_targets = _read_targets_from_error(previous_error)
    fix_hint["additional_read_targets"] = read_targets

    return (
        {
            "error_kind": error_kind,
            "diagnosis": diagnosis,
            "fix_hint": fix_hint,
        },
        read_targets,
        warnings,
    )
