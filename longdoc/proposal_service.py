from __future__ import annotations

import difflib
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

_TEXTUAL_PATCH_OPS = {"patch_file_lines", "replace_text", "update_section", "append_to_file"}
_TEXTUAL_CONTENT_KEYS = ("new_content", "content", "new_text")
# Typst: matches both `#include "x"` (word boundary fires between `#` and `i`)
# and the expression form `include "x"` (e.g. `#let x = include "sections/x.typ"`).
_TYPST_INCLUDE_RE = re.compile(r'\b(?:include|import)\s+"([^"\n]+)"')
_LATEX_INCLUDE_RE = re.compile(r'\\(?:input|include|subfile)\s*\{\s*([^}\n]+?)\s*\}')


def _strip_line_comment(line: str) -> str:
    # Typst `//`
    cut = line.find("//")
    if cut >= 0:
        line = line[:cut]
    # LaTeX `%` (skip escaped \%).
    i = 0
    while i < len(line):
        if line[i] == "%" and (i == 0 or line[i - 1] != "\\"):
            line = line[:i]
            break
        i += 1
    return line


def _extract_include_targets(text: str) -> list[str]:
    if not isinstance(text, str) or not text:
        return []
    matches: list[str] = []
    for raw_line in text.splitlines():
        line = _strip_line_comment(raw_line)
        matches.extend(_TYPST_INCLUDE_RE.findall(line))
        matches.extend(_LATEX_INCLUDE_RE.findall(line))
    return matches


def _resolve_relative_include(parent_filename: str, include_target: str) -> str:
    target = include_target.strip()
    if not target:
        return ""
    if "." not in Path(target).name and Path(parent_filename).suffix.lower() == ".tex":
        # \input{foo} without extension → foo.tex
        target = f"{target}.tex"
    try:
        parent_dir = Path(parent_filename).parent
        resolved = (parent_dir / target).as_posix()
    except Exception:
        return target
    # Normalize "./" and similar
    return Path(resolved).as_posix()

import secrets

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError
from django.utils import timezone

from SmartTeX.markup import MarkupType

from .document_graph import inspect_document_graph, introduced_graph_errors
from .locks import ProjectLockedError, get_locking_change_proposal, get_locking_session
from .models import AISession, ChangeProposal, ChangeProposalDiffAnnotation, ProjectAnnotation, ProjectOutlineItem, ProjectTask
from .services import serialize_diff_annotation
from .session_service import (
    SessionWriteError,
    _commit_worktree_change,
    _safe_session_rel_path,
    compile_session,
    create_session,
    discard_session,
    finalize_batch,
    generate_diff,
    write_to_session,
)
from small_model.services.do_not_touch import validate_do_not_touch
from small_model.services.policy_engine import ProposalPolicyEngine


PROPOSAL_PATCH_OPS = {
    "replace_text",
    "patch_file_lines",
    "append_to_file",
    "update_section",
    "create_new_file",
    "include_file",
}
SOURCE_EXTENSIONS = {".tex", ".typ"}

# Machine-readable schema for proposal patch ops. Returned in error payloads
# so the agent can recover deterministically instead of guessing field shapes.
PATCH_OP_SCHEMA: dict[str, dict[str, Any]] = {
    "patch_file_lines": {
        "required": ["op", "filename", "start_line", "end_line", "new_content"],
        "description": "Replace a contiguous line range in an existing file.",
    },
    "replace_text": {
        "required": ["op", "filename", "old_text", "new_text"],
        "description": "Exact-once textual replacement in an existing file.",
    },
    "append_to_file": {
        "required": ["op", "filename", "content"],
        "description": "Append content to EOF (or after anchor_section) of an existing file.",
    },
    "update_section": {
        "required": ["op", "filename", "section_index", "new_content"],
        "description": "Replace a parsed section by index in an existing source file.",
    },
    "create_new_file": {
        "required": ["op", "filename", "content"],
        "description": "Create a new file. New source files must also be wired into the document graph in the same proposal.",
    },
    "include_file": {
        "required": ["op", "filename", "include_target", "anchor_after OR anchor_before"],
        "description": (
            "Insert an #include / \\input directive for include_target into the parent file `filename` "
            "at the given anchor. This is NOT a declaration that an include already exists — it modifies "
            "the parent file. If a patch_file_lines op already adds a #include / \\input directive for "
            "the new file, include_file is not required."
        ),
    },
}

# Obvious typos / legacy names. Only honored by validate_document_change (not
# propose_document_change) and always reported in `normalizations`.
OP_ALIASES: dict[str, str] = {
    "patch_lines": "patch_file_lines",
}


def _proposal_expire_hours() -> int:
    return int(getattr(settings, "SESSION_EXPIRE_HOURS", getattr(settings, "LONGDOC_SESSION_EXPIRE_HOURS", 72)))


def _max_patch_lines() -> int:
    return int(getattr(settings, "MCP_MAX_PATCH_LINES", 50))


def _max_files() -> int:
    return int(getattr(settings, "MCP_MAX_SESSION_FILES", 5))


def _max_proposal_lines() -> int:
    return int(getattr(settings, "MCP_MAX_PROPOSAL_LINES", 500))


def _max_new_file_lines() -> int:
    return int(getattr(settings, "MCP_MAX_NEW_FILE_LINES", 200))


# ── validation-token cache ────────────────────────────────────────────────
#
# When validate_document_change returns valid:true we cache the normalized ops
# under a short-lived token so the agent can submit the same change via
# propose_document_change(validation_token=...) without re-sending the full
# patch_ops payload. The token is bound to the project's current HEAD sha, so
# any change to the project (another agent writing, user accepting a different
# proposal) automatically invalidates it.

_VALIDATION_TOKEN_TTL = 600  # 10 minutes


def _validation_token_cache_key(token: str) -> str:
    return f"longdoc:validation_token:{token}"


def _project_head_sha(project) -> str:
    from projects.services import _run_project_git
    try:
        proc = _run_project_git(project, ["rev-parse", "HEAD"], check=False)
    except Exception:
        return ""
    return (proc.stdout or "").strip()


def _mint_validation_token(
    project,
    *,
    goal: str,
    normalized_ops: list[dict[str, Any]],
) -> str | None:
    head_sha = _project_head_sha(project)
    if not head_sha:
        return None
    token = secrets.token_urlsafe(16)
    cache.set(
        _validation_token_cache_key(token),
        {
            "project_id": project.id,
            "goal": goal,
            "normalized_ops": normalized_ops,
            "head_sha": head_sha,
        },
        timeout=_VALIDATION_TOKEN_TTL,
    )
    return token


def _consume_validation_token(project, token: str) -> dict[str, Any]:
    """Look up a validation token and return its cached payload.

    Raises SessionWriteError with a stable error code on miss / stale / mismatch
    so the agent can recover deterministically (revalidate, then retry).
    """
    key = _validation_token_cache_key(token)
    cached = cache.get(key)
    if not cached:
        raise SessionWriteError(
            "INVALID_VALIDATION_TOKEN",
            "validation_token is unknown or expired.",
            status_code=409,
            suggestion=(
                f"Call validate_document_change again to obtain a fresh token "
                f"(tokens live for {_VALIDATION_TOKEN_TTL // 60} minutes)."
            ),
        )
    if cached.get("project_id") != project.id:
        raise SessionWriteError(
            "INVALID_VALIDATION_TOKEN",
            "validation_token belongs to a different project.",
            status_code=409,
            suggestion="Use the project_id that was passed to validate_document_change.",
        )
    current_sha = _project_head_sha(project)
    if not current_sha or cached.get("head_sha") != current_sha:
        # HEAD moved — the cached ops may now reference stale line numbers.
        cache.delete(key)
        raise SessionWriteError(
            "STALE_VALIDATION_TOKEN",
            "Project HEAD changed since the token was issued.",
            status_code=409,
            suggestion=(
                "The project was modified after validation. Call "
                "validate_document_change again with the current file contents."
            ),
            details={"head_sha_when_validated": cached.get("head_sha"), "head_sha_now": current_sha},
        )
    return cached


def _line_count(value: Any) -> int:
    if not isinstance(value, str) or value == "":
        return 0
    return max(1, len(value.splitlines()))


def _estimate_changed_lines(op: dict[str, Any]) -> int:
    # Use max(old, new) instead of the sum: a 30→30 rewrite is 30 lines of
    # change, not 60. Summing punishes equal-size rewrites that don't actually
    # exceed the per-op edit budget.
    name = str(op.get("op") or "")
    if name == "replace_text":
        return max(_line_count(op.get("old_text")), _line_count(op.get("new_text")))
    if name == "patch_file_lines":
        start = int(op.get("start_line") or 0)
        end = int(op.get("end_line") or 0)
        return max(max(0, end - start + 1), _line_count(op.get("new_content")))
    if name in {"append_to_file", "create_new_file", "update_section"}:
        return _line_count(op.get("content") or op.get("new_content"))
    if name == "include_file":
        return 1
    return 0


def _unknown_op_error(op: str) -> SessionWriteError:
    allowed = sorted(PROPOSAL_PATCH_OPS)
    candidates = difflib.get_close_matches(op, allowed, n=1, cutoff=0.5)
    did_you_mean = candidates[0] if candidates else OP_ALIASES.get(op)
    suggestion = (
        f"Use one of allowed_ops. Did you mean `{did_you_mean}`?"
        if did_you_mean
        else "Use one of allowed_ops. For line-range edits use patch_file_lines; "
             "for full-file rewrites use patch_file_lines with start_line=1 and end_line=<file length>."
    )
    details: dict[str, Any] = {
        "allowed_ops": allowed,
        "patch_op_schema": PATCH_OP_SCHEMA,
    }
    if did_you_mean:
        details["did_you_mean"] = did_you_mean
    return SessionWriteError(
        "UNKNOWN_OP",
        f"Unknown proposal operation: {op}",
        suggestion=suggestion,
        details=details,
    )


def _validate_include_file_fields(raw: dict[str, Any], idx: int) -> None:
    """Field-specific validation for include_file BEFORE filename path checks.

    Returns rich machine-readable errors so the agent can fix exactly the wrong
    field instead of guessing at the op shape.
    """
    missing: list[str] = []
    if not str(raw.get("filename") or "").strip():
        missing.append("filename")
    if not str(raw.get("include_target") or "").strip():
        missing.append("include_target")
    after = bool(str(raw.get("anchor_after") or "").strip()) if raw.get("anchor_after") is not None else False
    before = bool(str(raw.get("anchor_before") or "").strip()) if raw.get("anchor_before") is not None else False
    example = {
        "op": "include_file",
        "filename": "main.typ",
        "include_target": "sections/introduction.typ",
        "anchor_after": "#show: coursework-v2.with(",
    }
    if missing:
        raise SessionWriteError(
            "INVALID_INCLUDE_FILE_OP",
            f"include_file op #{idx} is missing required field(s): {', '.join(missing)}.",
            suggestion=(
                "include_file inserts an #include / \\input directive for include_target into the parent "
                "file `filename`. It is not a declaration that an include already exists."
            ),
            details={
                "missing_fields": missing,
                "required_fields": ["filename", "include_target", "anchor_after OR anchor_before"],
                "example": example,
                "patch_op_schema": {"include_file": PATCH_OP_SCHEMA["include_file"]},
            },
        )
    if after and before:
        raise SessionWriteError(
            "INVALID_INCLUDE_ANCHOR",
            "include_file requires exactly one of anchor_after or anchor_before.",
            suggestion="Pick a single insertion side and remove the other anchor.",
            details={
                "provided": {"anchor_after": True, "anchor_before": True},
                "required_fields": ["filename", "include_target", "anchor_after OR anchor_before"],
                "example": example,
            },
        )
    if not after and not before:
        raise SessionWriteError(
            "INVALID_INCLUDE_ANCHOR",
            "include_file requires exactly one of anchor_after or anchor_before.",
            suggestion="Read the parent file and choose one exact line as the insertion anchor.",
            details={
                "provided": {"anchor_after": False, "anchor_before": False},
                "required_fields": ["filename", "include_target", "anchor_after OR anchor_before"],
                "example": example,
            },
        )


def _normalize_patch_ops(
    patch_ops: Any,
    *,
    allow_aliases: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(patch_ops, list) or not patch_ops:
        raise SessionWriteError("INVALID_PATCH_OPS", "patch_ops must be a non-empty list.")
    normalized: list[dict[str, Any]] = []
    normalizations: list[dict[str, Any]] = []
    changed_files: set[str] = set()
    created_source_files: set[str] = set()
    included_files: set[str] = set()
    total_lines = 0

    for idx, raw in enumerate(patch_ops, start=1):
        if not isinstance(raw, dict):
            raise SessionWriteError("INVALID_PATCH_OP", f"patch_ops[{idx}] must be an object.")
        op = str(raw.get("op") or "").strip()
        if op not in PROPOSAL_PATCH_OPS:
            if allow_aliases and op in OP_ALIASES:
                new_op = OP_ALIASES[op]
                normalizations.append(
                    {
                        "type": "rename_op_alias",
                        "op_index": idx,
                        "from": op,
                        "to": new_op,
                        "reason": f"Renamed op `{op}` to `{new_op}`.",
                    }
                )
                raw = {**raw, "op": new_op}
                op = new_op
            else:
                raise _unknown_op_error(op)
        # For include_file, validate fields BEFORE path-normalizing so missing
        # filename/include_target/anchor produce field-specific errors rather
        # than the generic INVALID_FILENAME from _safe_session_rel_path.
        if op == "include_file":
            _validate_include_file_fields(raw, idx)
        if op == "update_section":
            if raw.get("section_index") is None:
                raise SessionWriteError(
                    "MISSING_FIELD",
                    f"patch_ops[{idx}]: update_section requires section_index.",
                    suggestion="Use list_project_sections to find the correct section_index before calling update_section.",
                )
            if "new_content" not in raw:
                raise SessionWriteError(
                    "MISSING_FIELD",
                    f"patch_ops[{idx}]: update_section requires new_content.",
                )
        rel = _safe_session_rel_path(str(raw.get("filename") or ""))
        item = {**raw, "filename": rel.as_posix(), "op": op}
        changed_files.add(item["filename"])

        changed = _estimate_changed_lines(item)
        if changed > _max_patch_lines() and op not in {"create_new_file"}:
            raise SessionWriteError(
                "PATCH_TOO_LARGE",
                f"{op} on {item['filename']} changes about {changed} lines; per-op limit is {_max_patch_lines()}.",
                status_code=413,
                suggestion="Split the change into smaller targeted operations.",
            )
        total_lines += changed

        if op == "create_new_file":
            content_lines = _line_count(item.get("content"))
            if content_lines > _max_new_file_lines():
                raise SessionWriteError(
                    "NEW_FILE_TOO_LARGE",
                    f"{item['filename']} has {content_lines} lines; new-file limit is {_max_new_file_lines()}.",
                    status_code=413,
                    suggestion=(
                        f"Split the file into two or more smaller files (each ≤ {_max_new_file_lines()} lines), "
                        f"each added by its own create_new_file op in the same proposal, "
                        f"and wire them in by adding #include / \\input directives in the parent file via patch_file_lines."
                    ),
                )
            if Path(item["filename"]).suffix.lower() in SOURCE_EXTENSIONS:
                created_source_files.add(item["filename"])

        if op == "include_file":
            # Field-level validation already ran in _validate_include_file_fields
            # before path normalization.
            target = _safe_session_rel_path(str(item.get("include_target") or "")).as_posix()
            item["include_target"] = target
            included_files.add(target)

        normalized.append(item)

    if len(changed_files) > _max_files():
        raise SessionWriteError(
            "PROPOSAL_FILE_LIMIT",
            f"Proposal touches {len(changed_files)} files; limit is {_max_files()}.",
            status_code=429,
            suggestion="Reduce the proposal scope and submit separate changes.",
        )
    if total_lines > _max_proposal_lines():
        raise SessionWriteError(
            "PROPOSAL_TOO_LARGE",
            f"Proposal changes about {total_lines} lines; limit is {_max_proposal_lines()}.",
            status_code=413,
            suggestion="Split the work into smaller proposals.",
        )
    # Also count textual #include / \input / \include directives added by
    # patch ops on existing files — they wire new source files into the
    # document graph just as well as an explicit include_file op.
    for item in normalized:
        if item["op"] not in _TEXTUAL_PATCH_OPS:
            continue
        for key in _TEXTUAL_CONTENT_KEYS:
            text = item.get(key)
            if not isinstance(text, str) or not text:
                continue
            for raw_target in _extract_include_targets(text):
                resolved = _resolve_relative_include(item["filename"], raw_target)
                if resolved:
                    included_files.add(resolved)
    missing_includes = sorted(created_source_files - included_files)
    if missing_includes:
        fname = missing_includes[0]
        detected = sorted(included_files)
        raise SessionWriteError(
            "SOURCE_FILE_NOT_INCLUDED",
            f"New source file {fname} must be included by the same proposal.",
            suggestion=(
                "Patch an existing compiled source file to add a #include / \\input directive for the new "
                "file, or add an include_file op with filename, include_target, and exactly one of "
                "anchor_after / anchor_before."
            ),
            details={
                "missing_includes": missing_includes,
                "detected_includes": detected,
                "hint": (
                    "Typst expression includes such as `#let x = include \"file.typ\"` are recognized by "
                    "the include detector — make sure the path inside the quotes matches the new file's "
                    "path relative to the parent file."
                ),
                "patch_op_schema": {
                    "include_file": PATCH_OP_SCHEMA["include_file"],
                    "patch_file_lines": PATCH_OP_SCHEMA["patch_file_lines"],
                },
            },
        )
    return normalized, normalizations


def _include_directive(project, include_target: str) -> str:
    if project.markup_type == MarkupType.TYPST:
        return f'#include "{include_target}"'
    return f"\\input{{{include_target}}}"


def _apply_include_file(session: AISession, op: dict[str, Any]) -> dict[str, Any]:
    rel = _safe_session_rel_path(op["filename"])
    target_path = Path(session.worktree_path) / rel
    if not target_path.exists():
        raise SessionWriteError("FILE_NOT_FOUND", f"{op['filename']} not found.")
    content = target_path.read_text(encoding="utf-8", errors="ignore")
    lines = content.splitlines(keepends=True)
    anchor_after = op.get("anchor_after")
    anchor_before = op.get("anchor_before")
    anchor = str(anchor_after or anchor_before)
    matches = [idx for idx, line in enumerate(lines) if anchor in line]
    if len(matches) != 1:
        if not matches:
            message = f"Anchor was not found in {op['filename']}; file may have changed since it was read."
            suggestion = "Re-read the file and choose an exact existing line as the include anchor."
        else:
            message = f"Anchor appears {len(matches)} times in {op['filename']}; exact-once match required."
            suggestion = "Re-read the file and choose a unique include anchor."
        raise SessionWriteError(
            "ANCHOR_MISMATCH",
            message,
            status_code=409,
            suggestion=f"{suggestion} Anchor: {anchor[:200]}",
        )
    directive = _include_directive(session.project, op["include_target"]) + "\n"
    idx = matches[0]
    insert_at = idx + 1 if anchor_after else idx
    lines.insert(insert_at, directive)
    target_path.write_text("".join(lines), encoding="utf-8")
    _commit_worktree_change(session, rel.as_posix(), op.get("change_summary") or f"include {op['include_target']}")
    return {"filename": rel.as_posix(), "op": "include_file", "include_target": op["include_target"]}


def _apply_patch_op(session: AISession, op: dict[str, Any]) -> dict[str, Any]:
    if op["op"] == "include_file":
        return _apply_include_file(session, op)
    params = {k: v for k, v in op.items() if k not in {"filename", "op", "change_summary"}}
    return write_to_session(
        session,
        op["filename"],
        op=op["op"],
        change_summary=str(op.get("change_summary") or ""),
        **params,
    )


def _changed_files_from_batch(session: AISession) -> list[dict[str, Any]]:
    try:
        changes = session.batch.changes.all()
    except Exception:
        return []
    return [
        {
            "filename": c.filename,
            "change_type": c.change_type,
            "lines_added": c.lines_added,
            "lines_removed": c.lines_removed,
        }
        for c in changes
    ]


def _serialize_compile_failure(result: dict[str, Any]) -> str:
    diagnostics = result.get("diagnostics")
    if isinstance(diagnostics, list) and diagnostics:
        first = diagnostics[0]
        if isinstance(first, dict):
            location = first.get("file") or first.get("filename") or ""
            line = first.get("line")
            message = first.get("message") or first.get("text") or ""
            prefix = f"{location}:{line}: " if location and line else f"{location}: " if location else ""
            return f"{prefix}{message}".strip()
    log = str(result.get("log") or "").strip()
    lines = [line.strip() for line in log.splitlines() if line.strip()]
    return "\n".join(lines[:12])[:4000]


def _retry_lock_suggestion(proposal: ChangeProposal) -> str:
    if (
        proposal.created_by == ChangeProposal.CreatedBy.MCP
        and proposal.status in (
            ChangeProposal.Status.FAILED_VALIDATION,
            ChangeProposal.Status.FAILED_COMPILE,
        )
    ):
        return (
            f"Retry via the proposal workflow: call validate_document_change or propose_document_change again "
            f"to iterate on failed MCP proposal #{proposal.id}. Do not use direct project file-write tools while "
            f"this proposal is active."
        )
    return f"Ask the user to review/discard proposal #{proposal.id} before trying again."


def _active_proposal(project) -> ChangeProposal | None:
    return get_locking_change_proposal(project)


def serialize_change_proposal(proposal: ChangeProposal | None) -> dict[str, Any] | None:
    if proposal is None:
        return None
    session = proposal.internal_session
    auto_discarded_id = getattr(proposal, "auto_discarded_previous_failed_proposal_id", None)
    payload: dict[str, Any] = {
        "id": proposal.id,
        "goal": proposal.goal,
        "status": proposal.status,
        "smcl_risk_level": proposal.smcl_risk_level or "low",
        "smcl_warnings": proposal.smcl_warnings or [],
        "validation_status": proposal.validation_status,
        "compile_status": proposal.compile_status,
        "compile_error_summary": proposal.compile_error_summary,
        "graph_validation_errors": proposal.graph_validation_errors,
        "user_visible_message": proposal.user_visible_message,
        "changed_files": proposal.changed_files,
        "addresses_outline_item_id": proposal.addresses_outline_item_id,
        "addresses_task_id": proposal.addresses_task_id,
        "preview_pdf_available": bool(session and session.staging_pdf_path),
        "created_by": proposal.created_by,
        "expires_at": proposal.expires_at.isoformat(),
        "accepted_at": proposal.accepted_at.isoformat() if proposal.accepted_at else None,
        "discarded_at": proposal.discarded_at.isoformat() if proposal.discarded_at else None,
        "created_at": proposal.created_at.isoformat(),
        "updated_at": proposal.updated_at.isoformat(),
    }
    try:
        payload["annotation_ids"] = list(session.batch.annotations_completed.values_list("id", flat=True)) if session and hasattr(session, "batch") else []
    except Exception:
        payload["annotation_ids"] = []
    payload["diff_annotations"] = [
        serialize_diff_annotation(item)
        for item in proposal.diff_annotations.order_by("status", "file_name", "line_number", "id")
    ]
    payload["open_diff_annotation_ids"] = [
        item["id"]
        for item in payload["diff_annotations"]
        if item.get("status") == ChangeProposalDiffAnnotation.Status.OPEN
    ]
    if auto_discarded_id is not None:
        payload["auto_discarded_previous_failed_proposal_id"] = auto_discarded_id
    return payload


def get_active_change_proposal(project) -> ChangeProposal | None:
    return _active_proposal(project)


def _prepare_proposal_session_for_iteration(project, proposal: ChangeProposal):
    from .session_service import create_session

    session = proposal.internal_session
    if session is None or session.status in (
        AISession.Status.ACCEPTED,
        AISession.Status.DISCARDED,
        AISession.Status.EXPIRED,
    ):
        session = create_session(project, proposal.goal, skip_lock_check=True)
        proposal.internal_session = session
        proposal.save(update_fields=["internal_session", "updated_at"])
        return session

    if session.status == AISession.Status.READY_FOR_REVIEW:
        session.status = AISession.Status.ACTIVE
        session.save(update_fields=["status", "updated_at"])
    return session


def _finalize_proposal_execution(
    proposal: ChangeProposal,
    *,
    project,
    user,
    normalized_ops: list[dict[str, Any]],
    annotations: list[ProjectAnnotation],
    diff_annotations: list[ChangeProposalDiffAnnotation] | None = None,
) -> ChangeProposal:
    baseline_graph = inspect_document_graph(project)
    session = _prepare_proposal_session_for_iteration(project, proposal)

    for op in normalized_ops:
        _apply_patch_op(session, op)

    proposed_graph = inspect_document_graph(project, root=Path(session.worktree_path))
    graph_errors = introduced_graph_errors(baseline_graph, proposed_graph)
    if graph_errors:
        proposal.status = ChangeProposal.Status.FAILED_VALIDATION
        proposal.validation_status = ChangeProposal.ValidationStatus.FAILED
        proposal.graph_validation_errors = graph_errors
        proposal.compile_status = AISession.CompileStatus.NOT_RUN
        proposal.compile_error_summary = ""
        proposal.diff_summary = ""
        proposal.changed_files = []
        proposal.user_visible_message = "Could not prepare this change"
        proposal.save(
            update_fields=[
                "status",
                "validation_status",
                "graph_validation_errors",
                "compile_status",
                "compile_error_summary",
                "diff_summary",
                "changed_files",
                "user_visible_message",
                "updated_at",
            ]
        )
        return proposal

    proposal.validation_status = ChangeProposal.ValidationStatus.PASSED
    proposal.graph_validation_errors = []
    proposal.save(update_fields=["validation_status", "graph_validation_errors", "updated_at"])

    compile_result = compile_session(session)
    session.refresh_from_db()
    proposal.compile_status = session.compile_status
    if compile_result.get("status") != "success" or session.compile_status != AISession.CompileStatus.SUCCESS:
        compile_policy = ProposalPolicyEngine.post_compile_check(user, project, proposal, compile_result)
        proposal.status = ChangeProposal.Status.FAILED_COMPILE
        proposal.compile_error_summary = _serialize_compile_failure(compile_result)
        proposal.diff_summary = generate_diff(session)
        proposal.changed_files = []
        proposal.smcl_risk_level = compile_policy.risk_level
        proposal.smcl_warnings = compile_policy.warnings
        proposal.smcl_metadata = {**(proposal.smcl_metadata or {}), **compile_policy.metadata}
        proposal.user_visible_message = (
            compile_policy.reason
            if compile_policy.action in {"stop_and_ask_user", "narrow_scope"}
            else "The document could not be compiled after applying the changes."
        )
        proposal.save(
            update_fields=[
                "status",
                "compile_status",
                "compile_error_summary",
                "diff_summary",
                "changed_files",
                "user_visible_message",
                "smcl_risk_level",
                "smcl_warnings",
                "smcl_metadata",
                "updated_at",
            ]
        )
        return proposal

    diff = generate_diff(session)
    post_policy = ProposalPolicyEngine.post_patch_check(user, project, proposal, diff)
    proposal.smcl_risk_level = post_policy.risk_level
    proposal.smcl_warnings = post_policy.warnings
    proposal.smcl_metadata = {**(proposal.smcl_metadata or {}), **post_policy.metadata}
    if post_policy.action == "reject":
        proposal.status = ChangeProposal.Status.FAILED_VALIDATION
        proposal.validation_status = ChangeProposal.ValidationStatus.FAILED
        proposal.graph_validation_errors = [
            {
                "error": "SMCL_DIFF_REJECTED",
                "message": post_policy.reason or "Suggested change was rejected by AI safety validation.",
            }
        ]
        proposal.compile_error_summary = ""
        proposal.diff_summary = ""
        proposal.changed_files = []
        proposal.user_visible_message = post_policy.reason or "Suggested change needs a narrower patch."
        proposal.save(
            update_fields=[
                "status",
                "validation_status",
                "graph_validation_errors",
                "compile_error_summary",
                "diff_summary",
                "changed_files",
                "user_visible_message",
                "smcl_risk_level",
                "smcl_warnings",
                "smcl_metadata",
                "updated_at",
            ]
        )
        return proposal

    finalize_batch(
        session,
        summary=proposal.goal,
        task_ids=[proposal.addresses_task_id] if proposal.addresses_task_id else None,
        annotation_ids=[item.id for item in annotations],
    )
    if diff_annotations:
        ChangeProposalDiffAnnotation.objects.filter(id__in=[item.id for item in diff_annotations]).update(
            status=ChangeProposalDiffAnnotation.Status.DONE,
            resolved_by_session=session,
            resolved_at=timezone.now(),
        )
    session.refresh_from_db()
    proposal.status = ChangeProposal.Status.READY_FOR_REVIEW
    proposal.compile_status = AISession.CompileStatus.SUCCESS
    proposal.compile_error_summary = ""
    proposal.diff_summary = diff
    proposal.changed_files = _changed_files_from_batch(session)
    proposal.user_visible_message = "Ready for review"
    proposal.save(
        update_fields=[
            "status",
            "compile_status",
            "compile_error_summary",
            "diff_summary",
            "changed_files",
            "user_visible_message",
            "smcl_risk_level",
            "smcl_warnings",
            "smcl_metadata",
            "updated_at",
        ]
    )
    return proposal


def propose_document_change(
    project,
    *,
    goal: str,
    patch_ops: list[dict[str, Any]] | None = None,
    addresses_task_id: int | None = None,
    addresses_outline_item_id: int | None = None,
    annotation_ids: list[int] | None = None,
    diff_annotation_ids: list[int] | None = None,
    created_by: str = ChangeProposal.CreatedBy.MCP,
    validation_token: str | None = None,
    continue_existing: bool = False,
) -> ChangeProposal:
    auto_discarded_previous_failed_proposal_id: int | None = None
    used_validation_token: str | None = None
    # Short-circuit: if the agent passes a token from a recent successful
    # validate_document_change, use the cached normalized_ops verbatim. This
    # both shaves a large duplicated payload off the prompt and protects
    # against accidental drift between validate and propose. Mismatch / staleness
    # raise stable error codes so the agent can re-validate and retry.
    if validation_token:
        cached = _consume_validation_token(project, validation_token)
        cached_ops = cached.get("normalized_ops") or []
        if patch_ops:
            # Detect actual drift from what was validated. We compare on a
            # canonical view so cosmetic dict-ordering doesn't trigger false
            # positives.
            import json as _json
            if _json.dumps(list(patch_ops), sort_keys=True) != _json.dumps(cached_ops, sort_keys=True):
                raise SessionWriteError(
                    "VALIDATION_TOKEN_OPS_MISMATCH",
                    "patch_ops provided alongside validation_token do not match the validated ops.",
                    status_code=409,
                    suggestion=(
                        "Either omit patch_ops when passing validation_token, or call "
                        "validate_document_change again on the modified ops to obtain a fresh token."
                    ),
                )
        patch_ops = cached_ops
        if not goal:
            goal = str(cached.get("goal") or "")
        used_validation_token = validation_token
    elif not patch_ops:
        raise SessionWriteError(
            "INVALID_PATCH_OPS",
            "patch_ops is required when validation_token is not provided.",
        )
    auto_discarded_previous_failed_proposal_id: int | None = None
    existing_proposal: ChangeProposal | None = None
    if get_locking_session(project) is not None or get_locking_change_proposal(project) is not None:
        session = get_locking_session(project)
        if session is not None:
            linked_proposal = getattr(session, "change_proposal", None)
            if not (
                continue_existing
                and linked_proposal is not None
                and linked_proposal.created_by == ChangeProposal.CreatedBy.MCP
                and created_by == ChangeProposal.CreatedBy.MCP
            ):
                raise ProjectLockedError(project=project, session=session)
            existing_proposal = linked_proposal
        proposal = get_locking_change_proposal(project)
        if (
            existing_proposal is None
            and continue_existing
            and proposal is not None
            and proposal.created_by == ChangeProposal.CreatedBy.MCP
            and created_by == ChangeProposal.CreatedBy.MCP
        ):
            existing_proposal = proposal
        if existing_proposal is not None:
            proposal = existing_proposal
            session = proposal.internal_session
            if session is not None and session.status == AISession.Status.READY_FOR_REVIEW:
                session.status = AISession.Status.ACTIVE
                session.save(update_fields=["status", "updated_at"])
            proposal.status = ChangeProposal.Status.VALIDATING
            proposal.validation_status = ChangeProposal.ValidationStatus.PENDING
            proposal.goal = str(goal or "").strip() or proposal.goal
            proposal.compile_status = AISession.CompileStatus.NOT_RUN
            proposal.compile_error_summary = ""
            proposal.graph_validation_errors = []
            proposal.user_visible_message = "Preparing suggested change..."
            proposal.save(
                update_fields=[
                    "status",
                    "validation_status",
                    "goal",
                    "compile_status",
                    "compile_error_summary",
                    "graph_validation_errors",
                    "user_visible_message",
                    "updated_at",
                ]
            )
        if existing_proposal is not None:
            proposal = existing_proposal
        elif proposal is not None:
            # If the only thing locking the project is an MCP-created proposal that
            # already failed validation/compile, auto-discard it so the agent can
            # iterate instead of spinning in PROJECT_LOCKED → cancel → PROJECT_LOCKED.
            # User-owned or pending/ready proposals still block — those are visible
            # to the user and must be acted on in the UI.
            auto_discard_ok = (
                created_by == ChangeProposal.CreatedBy.MCP
                and proposal.created_by == ChangeProposal.CreatedBy.MCP
                and proposal.status in (
                    ChangeProposal.Status.FAILED_VALIDATION,
                    ChangeProposal.Status.FAILED_COMPILE,
                )
            )
            if auto_discard_ok:
                try:
                    cancel_change_proposal(proposal)
                    auto_discarded_previous_failed_proposal_id = proposal.id
                except SessionWriteError:
                    pass
        # Re-check the lock — auto-discard may have cleared it.
        if existing_proposal is None and (get_locking_session(project) is not None or get_locking_change_proposal(project) is not None):
            session = get_locking_session(project)
            if session is not None:
                raise ProjectLockedError(project=project, session=session)
            blocking = get_locking_change_proposal(project)
            raise SessionWriteError(
                "PROJECT_LOCKED",
                f"Project {project.id} already has suggested change #{blocking.id} in status {blocking.status}: {blocking.goal}",
                status_code=423,
                suggestion=_retry_lock_suggestion(blocking),
            )

    user = project.owner
    # Deterministic, cheap validation first — avoid burning an LLM call on a
    # proposal that will be rejected by static limits (file count, line count,
    # per-op size, unknown ops, missing includes). propose_document_change does
    # NOT honor op aliases; the agent must submit the canonical op name.
    normalized_ops, _normalizations = _normalize_patch_ops(patch_ops, allow_aliases=False)
    validate_do_not_touch(project, normalized_ops)

    pre_policy = ProposalPolicyEngine.pre_proposal_check(user, project, str(goal or ""))
    if pre_policy.action == "stop":
        raise SessionWriteError(
            "SMCL_SCOPE_CLARIFICATION_REQUIRED",
            pre_policy.reason or "Please clarify the requested edit scope.",
            status_code=409,
            suggestion="Clarify the scope and submit a narrower proposal.",
        )
    expires_at = timezone.now() + timedelta(hours=_proposal_expire_hours())
    task = ProjectTask.objects.filter(project=project, id=addresses_task_id).first() if addresses_task_id else None
    outline_item = (
        ProjectOutlineItem.objects.filter(project=project, id=addresses_outline_item_id).first()
        if addresses_outline_item_id
        else None
    )
    normalized_annotation_ids = [int(value) for value in (annotation_ids or [])]
    annotations = list(ProjectAnnotation.objects.filter(project=project, id__in=normalized_annotation_ids)) if normalized_annotation_ids else []
    found_annotation_ids = {item.id for item in annotations}
    missing_annotation_ids = [item_id for item_id in normalized_annotation_ids if item_id not in found_annotation_ids]
    if missing_annotation_ids:
        raise SessionWriteError(
            "ANNOTATIONS_NOT_FOUND",
            f"Some annotation ids do not exist in this project: {missing_annotation_ids}",
            status_code=404,
            suggestion="Call list_annotations for this project and retry with only valid annotation ids.",
        )
    normalized_diff_annotation_ids = [int(value) for value in (diff_annotation_ids or [])]
    diff_annotations: list[ChangeProposalDiffAnnotation] = []
    if normalized_diff_annotation_ids:
        if existing_proposal is None:
            raise SessionWriteError(
                "DIFF_ANNOTATIONS_REQUIRE_EXISTING_PROPOSAL",
                "diff_annotation_ids can only be resolved while updating an existing proposal.",
                status_code=409,
                suggestion="Call propose_document_change with continue_existing=true for the active proposal, or omit diff_annotation_ids.",
            )
        diff_annotations = list(
            ChangeProposalDiffAnnotation.objects.filter(
                proposal=existing_proposal,
                id__in=normalized_diff_annotation_ids,
            )
        )
        found_diff_annotation_ids = {item.id for item in diff_annotations}
        missing_diff_annotation_ids = [item_id for item_id in normalized_diff_annotation_ids if item_id not in found_diff_annotation_ids]
        if missing_diff_annotation_ids:
            raise SessionWriteError(
                "DIFF_ANNOTATIONS_NOT_FOUND",
                f"Some diff annotation ids do not exist on the active proposal: {missing_diff_annotation_ids}",
                status_code=404,
                suggestion="Call the proposal status/diff endpoint and retry with only valid open diff_annotation ids.",
            )

    if existing_proposal is None:
        try:
            proposal = ChangeProposal.objects.create(
                project=project,
                goal=str(goal or "").strip() or "Suggested change",
                status=ChangeProposal.Status.VALIDATING,
                validation_status=ChangeProposal.ValidationStatus.PENDING,
                patch_ops=normalized_ops,
                smcl_metadata=pre_policy.metadata,
                addresses_task=task,
                addresses_outline_item=outline_item,
                created_by=created_by,
                expires_at=expires_at,
                user_visible_message="Preparing suggested change...",
            )
            # Transient attribute — serializer surfaces it so the agent can log
            # that the previous failed MCP proposal was auto-discarded.
            proposal.auto_discarded_previous_failed_proposal_id = auto_discarded_previous_failed_proposal_id
        except IntegrityError:
            active = get_locking_change_proposal(project)
            if active is not None:
                message = f"This project already has pending suggested change #{active.id}: {active.goal}"
                suggestion = f"Ask the user to review/discard proposal #{active.id} in the UI before submitting another."
            else:
                message = "This project already has a pending suggested change."
                suggestion = "Ask the user to review/discard the existing proposal in the UI before submitting another."
            raise SessionWriteError(
                "PROJECT_LOCKED",
                message,
                status_code=423,
                suggestion=suggestion,
            )
    else:
        proposal = existing_proposal
        proposal.goal = str(goal or "").strip() or proposal.goal
        proposal.patch_ops = [*(proposal.patch_ops or []), *normalized_ops]
        proposal.smcl_metadata = {**(proposal.smcl_metadata or {}), **pre_policy.metadata}
        proposal.addresses_task = task
        proposal.addresses_outline_item = outline_item
        proposal.save(
            update_fields=[
                "goal",
                "patch_ops",
                "smcl_metadata",
                "addresses_task",
                "addresses_outline_item",
                "updated_at",
            ]
        )

    try:
        return _finalize_proposal_execution(
            proposal,
            project=project,
            user=user,
            normalized_ops=normalized_ops,
            annotations=annotations,
            diff_annotations=diff_annotations,
        )

    except SessionWriteError as exc:
        if proposal.internal_session_id:
            proposal.internal_session.refresh_from_db()
        proposal.status = ChangeProposal.Status.FAILED_VALIDATION
        proposal.validation_status = ChangeProposal.ValidationStatus.FAILED
        proposal.graph_validation_errors = [exc.payload()]
        proposal.compile_status = AISession.CompileStatus.NOT_RUN
        proposal.compile_error_summary = ""
        proposal.diff_summary = ""
        proposal.changed_files = []
        proposal.user_visible_message = "Could not prepare this change"
        proposal.save(
            update_fields=[
                "status",
                "validation_status",
                "graph_validation_errors",
                "compile_status",
                "compile_error_summary",
                "diff_summary",
                "changed_files",
                "user_visible_message",
                "updated_at",
            ]
        )
        return proposal



# ── Whole-proposal preview/validation ──────────────────────────────────────


def _patch_lines_span(op: dict[str, Any]) -> int:
    start = int(op.get("start_line") or 0)
    end = int(op.get("end_line") or 0)
    return max(0, end - start + 1)


def _patch_lines_delta(op: dict[str, Any]) -> int:
    if str(op.get("op") or "") != "patch_file_lines":
        return 0
    return _line_count(op.get("new_content")) - _patch_lines_span(op)


def _line_patch_drift_context(ops: list[dict[str, Any]], failed_index: int, filename: str) -> list[dict[str, Any]]:
    context: list[dict[str, Any]] = []
    for idx, op in enumerate(ops[: max(0, failed_index - 1)], start=1):
        if str(op.get("op") or "") != "patch_file_lines" or str(op.get("filename") or "") != filename:
            continue
        context.append(
            {
                "op_index": idx,
                "start_line": op.get("start_line"),
                "end_line": op.get("end_line"),
                "old_span_lines": _patch_lines_span(op),
                "new_content_lines": _line_count(op.get("new_content")),
                "line_delta": _patch_lines_delta(op),
            }
        )
    return context[-8:]


def _normalize_patch_line_order(
    ops: list[dict[str, Any]],
    *,
    auto_reorder_line_patches: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Optionally reorder independent line-range edits bottom-up per file.

    This is deliberately conservative: only patch_file_lines ops are reordered,
    and only when all non-line ops are left in their original relative order.
    The returned operation list is suitable for a follow-up real proposal call.
    """
    if not auto_reorder_line_patches:
        return ops, []

    line_groups: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for idx, op in enumerate(ops):
        if str(op.get("op") or "") == "patch_file_lines":
            line_groups.setdefault(str(op.get("filename") or ""), []).append((idx, op))

    sorted_groups: dict[str, list[dict[str, Any]]] = {}
    normalizations: list[dict[str, Any]] = []
    for filename, items in line_groups.items():
        original = [op for _, op in items]
        sorted_ops = sorted(
            original,
            key=lambda item: (int(item.get("start_line") or 0), int(item.get("end_line") or 0)),
            reverse=True,
        )
        sorted_groups[filename] = list(sorted_ops)
        if original != sorted_ops and len(original) > 1:
            normalizations.append(
                {
                    "type": "reorder_patch_file_lines_bottom_up",
                    "filename": filename,
                    "reason": "Avoid line-number drift when multiple patch_file_lines ops edit the same file.",
                    "original_order": [
                        {"start_line": op.get("start_line"), "end_line": op.get("end_line")} for op in original
                    ],
                    "new_order": [
                        {"start_line": op.get("start_line"), "end_line": op.get("end_line")} for op in sorted_ops
                    ],
                }
            )

    cursors = {filename: 0 for filename in sorted_groups}
    reordered: list[dict[str, Any]] = []
    for op in ops:
        if str(op.get("op") or "") != "patch_file_lines":
            reordered.append(op)
            continue
        filename = str(op.get("filename") or "")
        cursor = cursors[filename]
        reordered.append(sorted_groups[filename][cursor])
        cursors[filename] = cursor + 1
    return reordered, normalizations


_VALIDATE_DIFF_BUDGET = 4000


def _truncate_diff_text(diff_text: str) -> tuple[str, bool]:
    """Cap diff_text at ~_VALIDATE_DIFF_BUDGET chars, ideally at a hunk boundary."""
    if not isinstance(diff_text, str) or len(diff_text) <= _VALIDATE_DIFF_BUDGET:
        return diff_text or "", False
    cut = diff_text.rfind("\n@@", 0, _VALIDATE_DIFF_BUDGET)
    if cut < 0:
        cut = diff_text.rfind("\n", 0, _VALIDATE_DIFF_BUDGET)
    if cut < 0:
        cut = _VALIDATE_DIFF_BUDGET
    return diff_text[:cut] + "\n… [diff truncated]\n", True


def _summarize_patch_ops(ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact echo of ops without body text — enough to confirm shape."""
    summary: list[dict[str, Any]] = []
    for op in ops:
        name = str(op.get("op") or "")
        item: dict[str, Any] = {"op": name, "filename": op.get("filename")}
        if name == "patch_file_lines":
            item["start_line"] = op.get("start_line")
            item["end_line"] = op.get("end_line")
            item["new_content_lines"] = _line_count(op.get("new_content"))
        elif name == "create_new_file":
            item["content_lines"] = _line_count(op.get("content"))
        elif name == "append_to_file":
            item["content_lines"] = _line_count(op.get("content"))
        elif name == "replace_text":
            item["old_text_lines"] = _line_count(op.get("old_text"))
            item["new_text_lines"] = _line_count(op.get("new_text"))
        elif name == "update_section":
            item["section_index"] = op.get("section_index")
            item["new_content_lines"] = _line_count(op.get("new_content"))
        elif name == "include_file":
            item["include_target"] = op.get("include_target")
            item["anchor_after"] = bool(op.get("anchor_after"))
            item["anchor_before"] = bool(op.get("anchor_before"))
        summary.append(item)
    return summary


def validate_document_change(
    project,
    *,
    goal: str,
    patch_ops: list[dict[str, Any]],
    compile_preview: bool = True,
    auto_reorder_line_patches: bool = True,
) -> dict[str, Any]:
    """Validate a whole proposal in a throwaway staging session.

    Unlike propose_document_change(), this does not create a ChangeProposal and
    does not leave a user-visible failed suggestion behind. It is intended for
    MCP agents to dry-run a complete multi-op edit plan before submitting the
    real proposal for user review.
    """
    if get_locking_session(project) is not None or get_locking_change_proposal(project) is not None:
        session = get_locking_session(project)
        if session is not None:
            raise ProjectLockedError(project=project, session=session)
        proposal = get_locking_change_proposal(project)
        # validate_document_change is a dry-run — it creates a throwaway
        # worktree and never mutates the project. Allow it to proceed even if
        # an MCP-created proposal already failed validation/compile, so the
        # agent can iterate on smaller ops instead of being forced to discard
        # the failed proposal just to dry-run. User-owned or pending/ready
        # proposals still block (those represent state the user must act on).
        permit = (
            proposal.created_by == ChangeProposal.CreatedBy.MCP
            and proposal.status in (
                ChangeProposal.Status.FAILED_VALIDATION,
                ChangeProposal.Status.FAILED_COMPILE,
            )
        )
        if not permit:
            raise SessionWriteError(
                "PROJECT_LOCKED",
                f"Project {project.id} already has suggested change #{proposal.id} in status {proposal.status}: {proposal.goal}",
                status_code=423,
                suggestion=_retry_lock_suggestion(proposal),
                details={"proposal_id": proposal.id, "proposal_goal": proposal.goal, "proposal_status": proposal.status},
            )

    user = project.owner
    normalized_ops, normalizations = _normalize_patch_ops(patch_ops, allow_aliases=True)
    normalized_ops, reorder_normalizations = _normalize_patch_line_order(
        normalized_ops,
        auto_reorder_line_patches=bool(auto_reorder_line_patches),
    )
    normalizations = list(normalizations) + list(reorder_normalizations)
    validate_do_not_touch(project, normalized_ops)

    pre_policy = ProposalPolicyEngine.pre_proposal_check(user, project, str(goal or ""))
    if pre_policy.action == "stop":
        raise SessionWriteError(
            "SMCL_SCOPE_CLARIFICATION_REQUIRED",
            pre_policy.reason or "Please clarify the requested edit scope.",
            status_code=409,
            suggestion="Clarify the scope and submit a narrower validation request.",
        )

    session = None
    compile_result: dict[str, Any] | None = None
    diff_text = ""
    try:
        baseline_graph = inspect_document_graph(project)
        session = create_session(
            project,
            str(goal or "").strip() or "Validate suggested change",
            skip_lock_check=True,
        )

        applied_ops: list[dict[str, Any]] = []
        for idx, op in enumerate(normalized_ops, start=1):
            try:
                result = _apply_patch_op(session, op)
            except SessionWriteError as exc:
                details = {
                    **exc.payload(),
                    "valid": False,
                    "failed_stage": "apply_patch_op",
                    "failed_op_index": idx,
                    "failed_op": op,
                }
                if str(op.get("op") or "") == "patch_file_lines":
                    details["previous_patch_file_lines_on_same_file"] = _line_patch_drift_context(
                        normalized_ops,
                        idx,
                        str(op.get("filename") or ""),
                    )
                    details["suggestion"] = details.get("suggestion") or (
                        "Line numbers may have drifted after earlier edits. Reorder patch_file_lines for this file bottom-up or use anchors."
                    )
                return details
            applied_ops.append({"op_index": idx, "result": result})

        proposed_graph = inspect_document_graph(project, root=Path(session.worktree_path))
        graph_errors = introduced_graph_errors(baseline_graph, proposed_graph)
        if graph_errors:
            return {
                "valid": False,
                "failed_stage": "document_graph",
                "graph_validation_errors": graph_errors,
                "applied_ops": applied_ops,
                "normalized_patch_ops": normalized_ops,
                "normalizations": normalizations,
            }

        if compile_preview:
            compile_result = compile_session(session)
            session.refresh_from_db()
            if compile_result.get("status") != "success" or session.compile_status != AISession.CompileStatus.SUCCESS:
                return {
                    "valid": False,
                    "failed_stage": "compile",
                    "compile": compile_result,
                    "compile_error_summary": _serialize_compile_failure(compile_result),
                    "applied_ops": applied_ops,
                    "normalized_patch_ops": normalized_ops,
                    "normalizations": normalizations,
                }

        diff_text = generate_diff(session)
        diff_text, diff_truncated = _truncate_diff_text(diff_text)
        final_goal = str(goal or "").strip() or "Suggested change"
        validation_token = _mint_validation_token(
            project,
            goal=final_goal,
            normalized_ops=normalized_ops,
        )
        payload: dict[str, Any] = {
            "valid": True,
            "will_apply": True,
            "goal": final_goal,
            "normalizations": normalizations,
            "compile": compile_result,
            "diff_text": diff_text,
            "changed_files": _changed_files_from_batch(session),
            "proposal_not_created": True,
        }
        if validation_token:
            payload["validation_token"] = validation_token
            payload["validation_token_ttl_seconds"] = _VALIDATION_TOKEN_TTL
        if diff_truncated:
            payload["diff_truncated"] = True
        token_hint = (
            " Prefer passing `validation_token` to propose_document_change instead of "
            "re-sending patch_ops — it skips duplicate payload and proves nothing drifted."
            if validation_token
            else ""
        )
        if normalizations:
            # Op shape was rewritten (alias rename, reorder). Agent must use
            # these instead of its original patch_ops — or just pass the token.
            payload["normalized_patch_ops"] = normalized_ops
            payload["suggestion"] = (
                "Validation changed the op shape — submit the returned normalized_patch_ops with "
                "propose_document_change (do not reuse your original patch_ops)." + token_hint
            )
        else:
            # Nothing was rewritten. Agent already has its ops; mirror only a
            # compact summary so the response stays small.
            payload["patch_ops_summary"] = _summarize_patch_ops(normalized_ops)
            payload["suggestion"] = (
                "No normalizations applied. Pass validation_token to propose_document_change "
                "(preferred), or resubmit your original patch_ops."
                if validation_token
                else "No normalizations applied. Resubmit your original patch_ops to "
                     "propose_document_change to create the user-visible suggestion."
            )
        return payload
    finally:
        if session is not None:
            try:
                discard_session(session)
            except Exception:
                pass

def cancel_change_proposal(proposal: ChangeProposal) -> ChangeProposal:
    if proposal.status not in (
        ChangeProposal.Status.DRAFT,
        ChangeProposal.Status.FAILED_VALIDATION,
        ChangeProposal.Status.FAILED_COMPILE,
    ):
        raise SessionWriteError(
            "PROPOSAL_LOCKED_FOR_REVIEW",
            f"Suggested change cannot be cancelled from status {proposal.status}.",
            status_code=403,
            suggestion="Ask the user to review or discard the pending change in the UI.",
        )
    if proposal.internal_session_id:
        session = proposal.internal_session
        if session.status not in (AISession.Status.DISCARDED, AISession.Status.ACCEPTED, AISession.Status.EXPIRED):
            discard_session(session)
    proposal.status = ChangeProposal.Status.DISCARDED
    proposal.discarded_at = timezone.now()
    proposal.user_visible_message = "Discarded"
    proposal.save(update_fields=["status", "discarded_at", "user_visible_message", "updated_at"])
    return proposal


def preview_patch(project, op: dict[str, Any]) -> dict[str, Any]:
    normalized_ops, _ = _normalize_patch_ops([op])
    normalized = normalized_ops[0]
    filename = normalized["filename"]
    from projects.services import project_dir

    target = project_dir(project) / filename
    original = target.read_text(encoding="utf-8", errors="ignore") if target.exists() else ""
    updated = original
    name = normalized["op"]
    if name == "create_new_file":
        if target.exists():
            raise SessionWriteError("FILE_EXISTS", f"{filename} already exists.")
        updated = str(normalized.get("content") or "")
    elif not target.exists():
        raise SessionWriteError("FILE_NOT_FOUND", f"{filename} not found.")
    elif name == "replace_text":
        old_text = str(normalized.get("old_text") or "")
        new_text = str(normalized.get("new_text") or "")
        count = original.count(old_text)
        if count != 1:
            raise SessionWriteError("NO_UNIQUE_MATCH", f"old_text matched {count} times; exact-once match required.")
        updated = original.replace(old_text, new_text, 1)
    elif name == "patch_file_lines":
        lines = original.splitlines(keepends=True)
        start = int(normalized.get("start_line") or 0)
        end = int(normalized.get("end_line") or 0)
        if start < 1 or end < start or end > len(lines):
            raise SessionWriteError("LINE_OUT_OF_RANGE", f"Lines {start}-{end} are out of range.")
        new_content = str(normalized.get("new_content") or "")
        replacement = new_content if new_content.endswith("\n") else new_content + "\n"
        updated = "".join(lines[: start - 1] + replacement.splitlines(keepends=True) + lines[end:])
    elif name == "append_to_file":
        insertion = str(normalized.get("content") or "")
        updated = original + ("" if original.endswith("\n") else "\n") + insertion
    elif name == "include_file":
        anchor = str(normalized.get("anchor_after") or normalized.get("anchor_before") or "")
        lines = original.splitlines(keepends=True)
        matches = [idx for idx, line in enumerate(lines) if anchor in line]
        if len(matches) != 1:
            if not matches:
                raise SessionWriteError("ANCHOR_MISMATCH", f"Anchor not found: {anchor[:200]}")
            raise SessionWriteError("ANCHOR_MISMATCH", f"Anchor appears {len(matches)} times: {anchor[:200]}")
        directive = _include_directive(project, normalized["include_target"]) + "\n"
        insert_at = matches[0] + 1 if normalized.get("anchor_after") else matches[0]
        lines.insert(insert_at, directive)
        updated = "".join(lines)
    else:
        raise SessionWriteError("UNSUPPORTED_PREVIEW_OP", f"preview_patch does not support {name}.")

    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
    )
    return {"filename": filename, "op": name, "will_apply": True, "diff": "".join(diff)}
