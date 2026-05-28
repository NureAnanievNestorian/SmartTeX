from __future__ import annotations

import difflib
from datetime import timedelta
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone

from SmartTeX.markup import MarkupType

from .document_graph import inspect_document_graph, introduced_graph_errors
from .locks import ProjectLockedError, get_locking_change_proposal, get_locking_session
from .models import AISession, ChangeProposal, ProjectOutlineItem, ProjectTask
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


def _line_count(value: Any) -> int:
    if not isinstance(value, str) or value == "":
        return 0
    return max(1, len(value.splitlines()))


def _estimate_changed_lines(op: dict[str, Any]) -> int:
    name = str(op.get("op") or "")
    if name == "replace_text":
        return _line_count(op.get("old_text")) + _line_count(op.get("new_text"))
    if name == "patch_file_lines":
        start = int(op.get("start_line") or 0)
        end = int(op.get("end_line") or 0)
        return max(0, end - start + 1) + _line_count(op.get("new_content"))
    if name in {"append_to_file", "create_new_file", "update_section"}:
        return _line_count(op.get("content") or op.get("new_content"))
    if name == "include_file":
        return 1
    return 0


def _normalize_patch_ops(patch_ops: Any) -> list[dict[str, Any]]:
    if not isinstance(patch_ops, list) or not patch_ops:
        raise SessionWriteError("INVALID_PATCH_OPS", "patch_ops must be a non-empty list.")
    normalized: list[dict[str, Any]] = []
    changed_files: set[str] = set()
    created_source_files: set[str] = set()
    included_files: set[str] = set()
    total_lines = 0

    for idx, raw in enumerate(patch_ops, start=1):
        if not isinstance(raw, dict):
            raise SessionWriteError("INVALID_PATCH_OP", f"patch_ops[{idx}] must be an object.")
        op = str(raw.get("op") or "").strip()
        if op not in PROPOSAL_PATCH_OPS:
            raise SessionWriteError("UNKNOWN_OP", f"Unknown proposal operation: {op}")
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
                    suggestion="Create a smaller included file or patch existing compiled sections directly.",
                )
            if Path(item["filename"]).suffix.lower() in SOURCE_EXTENSIONS:
                created_source_files.add(item["filename"])

        if op == "include_file":
            target = _safe_session_rel_path(str(item.get("include_target") or "")).as_posix()
            item["include_target"] = target
            after = item.get("anchor_after")
            before = item.get("anchor_before")
            if bool(after) == bool(before):
                raise SessionWriteError(
                    "INVALID_INCLUDE_ANCHOR",
                    "include_file requires exactly one of anchor_after or anchor_before.",
                    suggestion="Read the parent file and choose one exact anchor.",
                )
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
    missing_includes = sorted(created_source_files - included_files)
    if missing_includes:
        fname = missing_includes[0]
        raise SessionWriteError(
            "SOURCE_FILE_NOT_INCLUDED",
            f"New source file {fname} must be included by an include_file operation in the same proposal.",
            suggestion="Add include_file with an exact anchor, or patch an existing compiled source file instead.",
        )
    return normalized


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
        raise SessionWriteError(
            "ANCHOR_MISMATCH",
            f"Anchor appears {len(matches)} times in {op['filename']}; exact-once match required.",
            status_code=409,
            suggestion="Re-read the file and choose a unique include anchor.",
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


def _active_proposal(project) -> ChangeProposal | None:
    return get_locking_change_proposal(project)


def serialize_change_proposal(proposal: ChangeProposal | None) -> dict[str, Any] | None:
    if proposal is None:
        return None
    session = proposal.internal_session
    return {
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


def get_active_change_proposal(project) -> ChangeProposal | None:
    return _active_proposal(project)


def propose_document_change(
    project,
    *,
    goal: str,
    patch_ops: list[dict[str, Any]],
    addresses_task_id: int | None = None,
    addresses_outline_item_id: int | None = None,
    created_by: str = ChangeProposal.CreatedBy.MCP,
) -> ChangeProposal:
    if get_locking_session(project) is not None or get_locking_change_proposal(project) is not None:
        session = get_locking_session(project)
        if session is not None:
            raise ProjectLockedError(project=project, session=session)
        proposal = get_locking_change_proposal(project)
        raise SessionWriteError(
            "PROJECT_LOCKED",
            f"Project {project.id} already has a suggested change in status {proposal.status}.",
            status_code=423,
            suggestion="Cancel the failed proposal or ask the user to review/discard the pending suggestion.",
        )

    user = project.owner
    pre_policy = ProposalPolicyEngine.pre_proposal_check(user, project, str(goal or ""))
    if pre_policy.action == "stop":
        raise SessionWriteError(
            "SMCL_SCOPE_CLARIFICATION_REQUIRED",
            pre_policy.reason or "Please clarify the requested edit scope.",
            status_code=409,
            suggestion="Clarify the scope and submit a narrower proposal.",
        )

    normalized_ops = _normalize_patch_ops(patch_ops)
    validate_do_not_touch(project, normalized_ops)
    expires_at = timezone.now() + timedelta(hours=_proposal_expire_hours())
    task = ProjectTask.objects.filter(project=project, id=addresses_task_id).first() if addresses_task_id else None
    outline_item = (
        ProjectOutlineItem.objects.filter(project=project, id=addresses_outline_item_id).first()
        if addresses_outline_item_id
        else None
    )

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
    except IntegrityError:
        raise SessionWriteError(
            "PROJECT_LOCKED",
            "This project already has a pending suggested change.",
            status_code=423,
            suggestion="Resolve the existing proposal before submitting another.",
        )

    try:
        baseline_graph = inspect_document_graph(project)
        session = create_session(project, proposal.goal, skip_lock_check=True)
        proposal.internal_session = session
        proposal.save(update_fields=["internal_session", "updated_at"])

        for op in normalized_ops:
            _apply_patch_op(session, op)

        proposed_graph = inspect_document_graph(project, root=Path(session.worktree_path))
        graph_errors = introduced_graph_errors(baseline_graph, proposed_graph)
        if graph_errors:
            try:
                discard_session(session)
            except Exception:
                pass
            proposal.status = ChangeProposal.Status.FAILED_VALIDATION
            proposal.validation_status = ChangeProposal.ValidationStatus.FAILED
            proposal.graph_validation_errors = graph_errors
            proposal.user_visible_message = "Could not prepare this change"
            proposal.save(
                update_fields=[
                    "status",
                    "validation_status",
                    "graph_validation_errors",
                    "user_visible_message",
                    "updated_at",
                ]
            )
            return proposal

        proposal.validation_status = ChangeProposal.ValidationStatus.PASSED
        proposal.save(update_fields=["validation_status", "updated_at"])

        compile_result = compile_session(session)
        session.refresh_from_db()
        proposal.compile_status = session.compile_status
        if compile_result.get("status") != "success" or session.compile_status != AISession.CompileStatus.SUCCESS:
            compile_policy = ProposalPolicyEngine.post_compile_check(user, project, proposal, compile_result)
            proposal.status = ChangeProposal.Status.FAILED_COMPILE
            proposal.compile_error_summary = _serialize_compile_failure(compile_result)
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
            try:
                discard_session(session)
            except Exception:
                pass
            proposal.status = ChangeProposal.Status.FAILED_VALIDATION
            proposal.validation_status = ChangeProposal.ValidationStatus.FAILED
            proposal.graph_validation_errors = [
                {
                    "error": "SMCL_DIFF_REJECTED",
                    "message": post_policy.reason or "Suggested change was rejected by AI safety validation.",
                }
            ]
            proposal.user_visible_message = post_policy.reason or "Suggested change needs a narrower patch."
            proposal.save(
                update_fields=[
                    "status",
                    "validation_status",
                    "graph_validation_errors",
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
        )
        session.refresh_from_db()
        proposal.status = ChangeProposal.Status.READY_FOR_REVIEW
        proposal.compile_status = AISession.CompileStatus.SUCCESS
        proposal.diff_summary = diff
        proposal.changed_files = _changed_files_from_batch(session)
        proposal.user_visible_message = "Ready for review"
        proposal.save(
            update_fields=[
                "status",
                "compile_status",
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

    except SessionWriteError as exc:
        if proposal.internal_session_id:
            try:
                discard_session(proposal.internal_session)
            except Exception:
                pass
        proposal.status = ChangeProposal.Status.FAILED_VALIDATION
        proposal.validation_status = ChangeProposal.ValidationStatus.FAILED
        proposal.graph_validation_errors = [exc.payload()]
        proposal.user_visible_message = "Could not prepare this change"
        proposal.save(
            update_fields=[
                "status",
                "validation_status",
                "graph_validation_errors",
                "user_visible_message",
                "updated_at",
            ]
        )
        return proposal


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
    normalized = _normalize_patch_ops([op])[0]
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
            raise SessionWriteError("ANCHOR_MISMATCH", f"Anchor appears {len(matches)} times.")
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
