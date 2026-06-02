"""
Backend engine for isolated AI writing sessions.

Each session creates a git worktree so the live project directory is never
checked out to the session branch. Writes are patch-based for existing
source files. Sessions can be compiled to produce a separate staging PDF
and diffed against the accepted project state.

Public API:
    create_session(project, goal)                   → AISession
    write_to_session(session, filename, *, op, …)   → dict
    compile_session(session)                        → dict
    generate_diff(session)                          → str
    finalize_batch(session, summary, task_ids)      → AIBatch
    accept_session(session, user)                   → None
    discard_session(session)                        → None
    expire_stale_sessions()                         → int
"""
from __future__ import annotations

import difflib
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone

from SmartTeX.markup import MarkupType
from projects.models import Project
from projects.services import (
    COMPILE_SEMAPHORE,
    _git_env,
    _git_executable,
    _run_project_git,
    _split_source_sections,
    build_compiler_cmd,
    ensure_project_git_repo,
    main_source_filename,
    parse_compile_diagnostics,
    project_dir,
)

from .locks import ProjectLockedError, assert_not_locked, get_locking_session
from .models import AIBatch, AIBatchChange, AISession, ChangeProposal, ProjectAnnotation, ProjectTask


logger = logging.getLogger(__name__)

SOURCE_EXTENSIONS: frozenset[str] = frozenset({".tex", ".typ"})
_TEXT_EXTENSIONS_FOR_DIFF: frozenset[str] = frozenset({
    ".tex", ".typ", ".sty", ".cls", ".bib", ".txt", ".md",
    ".csv", ".json", ".yaml", ".yml", ".csl",
})
PATCH_OPS: frozenset[str] = frozenset({"patch_file_lines", "replace_text", "append_to_file", "update_section"})


# ── Error type ─────────────────────────────────────────────────────────────

@dataclass
class SessionWriteError(RuntimeError):
    error: str
    message: str
    status_code: int = 400
    suggestion: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        p: dict[str, Any] = {"error": self.error, "message": self.message}
        if self.suggestion:
            p["suggestion"] = self.suggestion
        if self.details:
            p.update(self.details)
        return p


# ── Path helpers ────────────────────────────────────────────────────────────

def session_dir(project: Project, session_id: int) -> Path:
    return project_dir(project) / ".smarttex" / "sessions" / str(session_id)


def _worktree_path(project: Project, session_id: int) -> Path:
    return session_dir(project, session_id) / "worktree"


def _staging_pdf_path(project: Project, session_id: int) -> Path:
    return session_dir(project, session_id) / "staging.pdf"


# ── Config helpers ──────────────────────────────────────────────────────────

def _session_expire_hours() -> int:
    return int(getattr(settings, "SESSION_EXPIRE_HOURS", 72))


def _session_max_files() -> int:
    return int(getattr(settings, "MCP_MAX_SESSION_FILES", 5))


# ── Git helpers ─────────────────────────────────────────────────────────────

def _run_worktree_git(worktree_path: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    git = _git_executable()
    if not git:
        raise RuntimeError("git executable not available")
    proc = subprocess.run(
        [git, *args],
        cwd=str(worktree_path),
        env=_git_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "git command failed").strip())
    return proc


def _branch_name(session_id: int) -> str:
    from datetime import datetime, UTC
    ts = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    return f"ai/session-{session_id}-{ts}"


def _get_session_changed_files(project: Project, session: AISession) -> set[str]:
    """Return the set of filenames changed in the session branch vs HEAD."""
    proc = _run_project_git(
        project,
        ["diff", "--name-only", "HEAD", session.branch_name],
        check=False,
    )
    if proc.returncode != 0:
        return set()
    return {line.strip() for line in (proc.stdout or "").splitlines() if line.strip()}


def _commit_worktree_change(session: AISession, rel_filename: str, message: str) -> None:
    worktree = Path(session.worktree_path)
    _run_worktree_git(worktree, ["add", rel_filename])
    status = _run_worktree_git(worktree, ["status", "--short", "--", rel_filename], check=False)
    if not (status.stdout or "").strip():
        return  # nothing staged → nothing to commit
    _run_worktree_git(worktree, ["commit", "--quiet", "-m", message])


# ── Guards ──────────────────────────────────────────────────────────────────

def _assert_session_writable(session: AISession) -> None:
    if session.status not in (AISession.Status.ACTIVE, AISession.Status.COMPILED):
        raise SessionWriteError(
            error="SESSION_NOT_ACTIVE",
            message=f"Session {session.id} cannot be written (status={session.status}).",
            status_code=409,
        )


def _safe_session_rel_path(filename: str) -> Path:
    raw = str(filename or "").strip().replace("\\", "/").lstrip("/")
    if not raw:
        raise SessionWriteError(error="INVALID_FILENAME", message="filename is required")
    rel = Path(raw)
    if rel.is_absolute() or any(part in {".", ".."} for part in rel.parts):
        raise SessionWriteError(error="INVALID_FILENAME", message="path traversal not allowed")
    return rel


# ── Patch helpers ────────────────────────────────────────────────────────────

def _patch_file_lines(
    path: Path,
    *,
    start_line: int,
    end_line: int,
    new_content: str,
    anchor_before: str | None = None,
    anchor_after: str | None = None,
) -> None:
    if not isinstance(new_content, str):
        raise SessionWriteError(error="INVALID_CONTENT", message="new_content must be a string")
    content = path.read_text(encoding="utf-8", errors="ignore")
    lines = content.splitlines(keepends=True)
    total = len(lines)
    s, e = int(start_line), int(end_line)
    if s < 1 or e < s or e > total:
        raise SessionWriteError(
            error="LINE_OUT_OF_RANGE",
            message=f"Lines {s}-{e} are out of range (file has {total} lines).",
        )
    if anchor_before is not None:
        actual = (lines[s - 2].rstrip("\n\r") if s >= 2 else "")
        if anchor_before.strip() not in actual:
            raise SessionWriteError(
                error="ANCHOR_MISMATCH",
                message=f"anchor_before not matched near line {s - 1}.",
                status_code=409,
                suggestion="Re-read the file to confirm the anchor text, then retry with updated line numbers.",
            )
    if anchor_after is not None:
        next_idx = e
        actual = (lines[next_idx].rstrip("\n\r") if next_idx < total else "")
        if anchor_after.strip() not in actual:
            raise SessionWriteError(
                error="ANCHOR_MISMATCH",
                message=f"anchor_after not matched near line {e + 1}.",
                status_code=409,
                suggestion="Re-read the file to confirm the anchor text, then retry with updated line numbers.",
            )
    replacement = new_content if new_content.endswith("\n") else new_content + "\n"
    updated = "".join(lines[: s - 1] + replacement.splitlines(keepends=True) + lines[e:])
    path.write_text(updated, encoding="utf-8")


def _replace_text(
    path: Path,
    *,
    old_text: str,
    new_text: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not old_text:
        raise SessionWriteError(error="INVALID_PARAMS", message="old_text is required")
    content = path.read_text(encoding="utf-8", errors="ignore")
    count = content.count(old_text)
    if count == 0:
        raise SessionWriteError(
            error="NO_MATCH",
            message=f"old_text not found in {path.name}.",
            suggestion="Re-read the file to confirm the exact text, then retry.",
        )
    if count > 1:
        raise SessionWriteError(
            error="AMBIGUOUS_MATCH",
            message=f"old_text appears {count} times in {path.name}. Exact-once match required.",
            status_code=409,
            suggestion="Add more context to old_text to make it unique, then retry.",
        )
    updated = content.replace(old_text, new_text, 1)
    if dry_run:
        diff_lines = difflib.unified_diff(
            content.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{path.name}",
            tofile=f"b/{path.name}",
        )
        return {"dry_run": True, "diff": "".join(diff_lines), "will_apply": True}
    path.write_text(updated, encoding="utf-8")
    return {"dry_run": False, "applied": True}


def _append_to_file(
    path: Path,
    *,
    content: str,
    anchor_section: str | None = None,
    project: Project | None = None,
) -> dict[str, Any]:
    existing = path.read_text(encoding="utf-8", errors="ignore")
    if anchor_section and project:
        chunks = _split_source_sections(project, existing)
        target = next(
            (c for c in chunks if c.title.strip().lower() == anchor_section.strip().lower()),
            None,
        )
        if target is None:
            raise SessionWriteError(
                error="SECTION_NOT_FOUND",
                message=f"Section '{anchor_section}' not found in {path.name}.",
                suggestion="Use list_project_sections to find the correct section title.",
            )
        lines = existing.splitlines(keepends=True)
        insertion = content if content.endswith("\n") else content + "\n"
        updated = "".join(lines[: target.end_line] + [insertion] + lines[target.end_line :])
        appended_at = target.end_line + 1
    else:
        prefix = "" if existing.endswith("\n") else "\n"
        insertion = content if content.endswith("\n") else content + "\n"
        updated = existing + prefix + insertion
        appended_at = len(updated.splitlines())
    path.write_text(updated, encoding="utf-8")
    return {"appended_at_line": appended_at}


def _update_section(
    path: Path,
    *,
    section_index: int,
    new_content: str,
    project: Project,
    **_: Any,
) -> None:
    source = path.read_text(encoding="utf-8", errors="ignore")
    chunks = _split_source_sections(project, source)
    target = next((c for c in chunks if c.index == section_index), None)
    if target is None:
        raise SessionWriteError(
            error="SECTION_NOT_FOUND",
            message=f"Section index {section_index} not found in {path.name}.",
            suggestion="Use list_project_sections to find the correct section index.",
        )
    lines = source.splitlines(keepends=True)
    replacement = new_content if new_content.endswith("\n") else new_content + "\n"
    updated = "".join(
        lines[: max(0, target.start_line - 1)]
        + replacement.splitlines(keepends=True)
        + lines[target.end_line :]
    )
    path.write_text(updated, encoding="utf-8")


# ── Public API ───────────────────────────────────────────────────────────────

def create_session(project: Project, goal: str, *, expires_hours: int | None = None, skip_lock_check: bool = False) -> AISession:
    """
    Create a new AI session: git branch + isolated worktree + AISession record.
    Raises ProjectLockedError if the project already has an active session.
    """
    if not skip_lock_check:
        assert_not_locked(project)

    if not _git_executable():
        raise RuntimeError("git executable is not available; cannot create AI session")

    hours = _session_expire_hours() if expires_hours is None else int(expires_hours)

    # Create the DB record first so we have an ID for path/branch naming.
    try:
        session = AISession.objects.create(
            project=project,
            goal=str(goal or "").strip() or "AI session",
            branch_name="PENDING",
            worktree_path="PENDING",
            status=AISession.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(hours=hours),
        )
    except IntegrityError:
        locking_session = get_locking_session(project)
        if locking_session is not None:
            raise ProjectLockedError(project=project, session=locking_session)
        raise

    try:
        ensure_project_git_repo(project)

        branch = _branch_name(session.id)
        wt_path = _worktree_path(project, session.id)
        wt_path.parent.mkdir(parents=True, exist_ok=True)

        _run_project_git(project, ["branch", branch, "HEAD"])
        _run_project_git(project, ["worktree", "add", str(wt_path), branch])

        session.branch_name = branch
        session.worktree_path = str(wt_path)
        session.save(update_fields=["branch_name", "worktree_path", "updated_at"])
    except Exception:
        try:
            session.delete()
        except Exception:
            logger.exception("Failed to delete session record after create_session failure")
        raise

    return session


def write_to_session(
    session: AISession,
    filename: str,
    *,
    op: str,
    change_summary: str = "",
    **params: Any,
) -> dict[str, Any]:
    """
    Apply a patch operation to a file in the session worktree and commit the result.

    op must be one of:
        create_new_file  — write a brand-new file (not yet in worktree)
        patch_file_lines — replace a line range (with optional anchors)
        replace_text     — exact-once text replacement (supports dry_run=True)
        append_to_file   — append to EOF or after a named section
        update_section   — replace a complete parsed section
    """
    _assert_session_writable(session)

    rel = _safe_session_rel_path(filename)
    str_rel = str(rel)
    project = session.project
    worktree = Path(session.worktree_path)
    target_path = worktree / rel

    is_source = target_path.suffix.lower() in SOURCE_EXTENSIONS
    file_exists = target_path.exists()

    # Enforce per-session file limit before touching anything.
    max_files = _session_max_files()
    changed_files = _get_session_changed_files(project, session)
    if str_rel not in changed_files and len(changed_files) >= max_files:
        raise SessionWriteError(
            error="SESSION_FILE_LIMIT",
            message=f"Session has already touched {len(changed_files)} files (limit: {max_files}).",
            status_code=429,
            suggestion="Complete this session before touching additional files.",
        )

    result: dict[str, Any] = {}

    if op == "create_new_file":
        if file_exists:
            raise SessionWriteError(
                error="FILE_EXISTS",
                message=f"{filename} already exists in the session worktree.",
                suggestion="Use patch_file_lines, replace_text, or update_section to edit existing files.",
            )
        content = params.pop("content", "")
        if not isinstance(content, str):
            raise SessionWriteError(error="INVALID_CONTENT", message="content must be a string")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")

    elif not file_exists:
        raise SessionWriteError(
            error="FILE_NOT_FOUND",
            message=f"{filename} not found. Use create_new_file to create it.",
        )

    elif is_source and op not in PATCH_OPS:
        raise SessionWriteError(
            error="USE_PATCH_TOOLS",
            message=f"Full-file overwrite of source file {filename} is not permitted in AI sessions.",
            suggestion="Use patch_file_lines, replace_text, update_section, or append_to_file.",
        )

    elif op == "patch_file_lines":
        _patch_file_lines(target_path, **params)

    elif op == "replace_text":
        patch_result = _replace_text(target_path, **params)
        if patch_result.get("dry_run"):
            return {"filename": str_rel, "op": op, **patch_result}
        result = patch_result

    elif op == "append_to_file":
        result = _append_to_file(target_path, project=project, **params)

    elif op == "update_section":
        _update_section(target_path, project=project, **params)

    else:
        raise SessionWriteError(error="UNKNOWN_OP", message=f"Unknown write operation: {op}")

    _commit_worktree_change(
        session,
        str_rel,
        change_summary or f"session({op}): {str_rel}",
    )
    return {"filename": str_rel, "op": op, **result}


def compile_session(session: AISession) -> dict[str, Any]:
    """
    Compile the session worktree and write the resulting PDF to the session directory.
    The staging PDF lives outside the worktree so it is never committed to the session branch.
    Updates session.compile_status, session.staging_pdf_path, and session.status.
    """
    if session.status not in (AISession.Status.ACTIVE, AISession.Status.COMPILED):
        raise SessionWriteError(
            error="SESSION_NOT_ACTIVE",
            message="Session must be active or already compiled to recompile.",
            status_code=409,
        )

    project = session.project
    worktree = Path(session.worktree_path)
    src_filename = main_source_filename(project)
    input_file = worktree / src_filename

    from projects.pre_compile import PreCompileResult, run_pre_compile_jobs
    import projects.plantuml_job  # noqa: F401 - registers PlantUmlJob
    import projects.pdf_embed_job  # noqa: F401 - registers PdfEmbedJob

    if not input_file.exists():
        msg = f"{src_filename} not found in session worktree"
        session.compile_status = AISession.CompileStatus.ERROR
        session.compile_log = msg
        session.save(update_fields=["compile_status", "compile_log", "updated_at"])
        return {"status": "error", "log": msg, "diagnostics": []}

    pre_compile_results = run_pre_compile_jobs(project, workdir=worktree)

    def _pre_compile_diagnostics(results: list[PreCompileResult]) -> list[dict]:
        diags: list[dict] = []
        for result in results:
            for error in result.errors:
                if ": " in error:
                    file_part, _, msg = error.partition(": ")
                else:
                    file_part, msg = "", error
                diags.append({
                    "file": file_part.strip(),
                    "line": 1,
                    "column": 1,
                    "severity": "error",
                    "message": f"[{result.job}] {msg.strip()}",
                })
        return diags

    sess_dir = session_dir(project, session.id)
    sess_dir.mkdir(parents=True, exist_ok=True)
    pdf_output = _staging_pdf_path(project, session.id)

    host_project_root = str(getattr(settings, "HOST_PROJECT_ROOT", "")).strip()
    if host_project_root:
        docker_mount_source = (
            Path(host_project_root)
            / "media"
            / "projects"
            / str(project.owner_id)
            / str(project.id)
            / ".smarttex"
            / "sessions"
            / str(session.id)
            / "worktree"
        )
    else:
        docker_mount_source = worktree

    (worktree / ".smarttex").mkdir(parents=True, exist_ok=True)

    cmd, run_kwargs, timeout = build_compiler_cmd(
        project.markup_type, src_filename, worktree, docker_mount_source
    )
    use_native_typst = project.markup_type == MarkupType.TYPST and bool(
        getattr(settings, "TYPST_USE_NATIVE", False)
    )

    acquired = COMPILE_SEMAPHORE.acquire(timeout=timeout)
    if not acquired:
        msg = "Compilation queue timeout"
        session.compile_status = AISession.CompileStatus.ERROR
        session.compile_log = msg
        session.save(update_fields=["compile_status", "compile_log", "updated_at"])
        return {"status": "error", "log": msg, "diagnostics": []}

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            **run_kwargs,
        )
        log_text = (proc.stdout or "") + "\n" + (proc.stderr or "")

        compiled_pdf = worktree / ".smarttex" / "main.pdf"
        if compiled_pdf.exists():
            shutil.move(str(compiled_pdf), str(pdf_output))

        # Remove LaTeX build artifacts from the worktree (don't pollute the session branch).
        for ext in (".aux", ".log", ".out", ".toc", ".synctex.gz", ".fls", ".fdb_latexmk", ".xdv", ".bbl", ".blg"):
            (worktree / ".smarttex" / f"main{ext}").unlink(missing_ok=True)
            (worktree / f"main{ext}").unlink(missing_ok=True)

        if pdf_output.exists():
            session.compile_status = AISession.CompileStatus.SUCCESS
            session.compile_log = log_text
            session.staging_pdf_path = str(pdf_output.relative_to(project_dir(project)))
            session.status = AISession.Status.COMPILED
            session.save(update_fields=["compile_status", "compile_log", "staging_pdf_path", "status", "updated_at"])
            return {
                "status": "success",
                "log": log_text,
                "diagnostics": _pre_compile_diagnostics(pre_compile_results) + parse_compile_diagnostics(project, log_text),
                "staging_pdf_path": session.staging_pdf_path,
            }

        session.compile_status = AISession.CompileStatus.ERROR
        session.compile_log = log_text
        session.save(update_fields=["compile_status", "compile_log", "updated_at"])
        return {
            "status": "error",
            "log": log_text,
            "diagnostics": _pre_compile_diagnostics(pre_compile_results) + parse_compile_diagnostics(project, log_text),
        }

    except subprocess.TimeoutExpired:
        msg = f"Compilation timed out after {timeout} seconds"
        session.compile_status = AISession.CompileStatus.ERROR
        session.compile_log = msg
        session.save(update_fields=["compile_status", "compile_log", "updated_at"])
        return {"status": "error", "log": msg, "diagnostics": []}

    except FileNotFoundError:
        msg = "typst binary not found" if use_native_typst else "Docker is not available"
        session.compile_status = AISession.CompileStatus.ERROR
        session.compile_log = msg
        session.save(update_fields=["compile_status", "compile_log", "updated_at"])
        return {"status": "error", "log": msg, "diagnostics": []}

    finally:
        COMPILE_SEMAPHORE.release()


def generate_diff(session: AISession) -> str:
    """
    Generate a unified diff of the session branch vs the project HEAD (the base state).
    Caches the result in AISession.diff_text and returns it.
    """
    project = session.project
    proc = _run_project_git(
        project,
        ["diff", "HEAD", session.branch_name, "--unified=3"],
        check=False,
    )
    diff = (proc.stdout or "").strip()
    if session.diff_text != diff:
        session.diff_text = diff
        session.save(update_fields=["diff_text", "updated_at"])
    return diff


def _render_puml_files(project: Project, proj_dir: Path, changed_files: list[str]) -> None:
    """After a session merge, render any .puml files to .svg and commit the result."""
    puml_files = [f for f in changed_files if Path(f).suffix.lower() == ".puml"]
    if not puml_files:
        return

    try:
        from projects.plantuml_job import _load_hashes, _save_hashes, _sha256, render_plantuml_svg
        from projects.services import commit_project_changes, ensure_project_dir
    except ImportError:
        logger.warning("plantuml_job not available — skipping auto-render on accept")
        return

    workdir = ensure_project_dir(project)
    hashes = _load_hashes(workdir)
    rendered: list[str] = []

    for puml_rel in puml_files:
        puml_path = proj_dir / puml_rel
        if not puml_path.exists():
            continue
        try:
            source = puml_path.read_text(encoding="utf-8")
            svg_bytes = render_plantuml_svg(source)
        except Exception as exc:
            logger.warning("plantuml render failed for %s in project %s: %s", puml_rel, project.id, exc)
            continue

        base = puml_rel.removesuffix(".puml")
        svg_rel = f"{base}.svg"
        svg_path = proj_dir / svg_rel
        svg_path.parent.mkdir(parents=True, exist_ok=True)
        svg_path.write_bytes(svg_bytes)
        hashes[puml_rel] = _sha256(source.encode("utf-8"))
        rendered.append(svg_rel)

    if not rendered:
        return

    _save_hashes(workdir, hashes)
    try:
        commit_project_changes(
            project,
            summary="Render PlantUML diagrams after session accept",
            operation="plantuml_render",
            source="web",
            target_files=rendered,
        )
    except Exception as exc:
        logger.warning("Failed to commit rendered SVGs for project %s: %s", project.id, exc)


def _cleanup_session_files(project: Project, session: AISession) -> None:
    """Remove worktree, delete git branch, and delete the session directory."""
    errors: list[str] = []

    wt = session.worktree_path
    if wt and wt != "PENDING":
        wt_path = Path(wt)
        try:
            _run_project_git(project, ["worktree", "remove", wt, "--force"], check=False)
        except Exception as exc:
            errors.append(f"worktree remove: {exc}")
        if wt_path.exists():
            shutil.rmtree(wt_path, ignore_errors=True)

    branch = session.branch_name
    if branch and branch != "PENDING":
        try:
            _run_project_git(project, ["branch", "-D", branch], check=False)
        except Exception as exc:
            errors.append(f"branch delete: {exc}")

    sess_dir = session_dir(project, session.id)
    if sess_dir.exists():
        shutil.rmtree(sess_dir, ignore_errors=True)

    if errors:
        logger.warning("Session %s cleanup encountered errors: %s", session.id, "; ".join(errors))


def finalize_batch(
    session: AISession,
    summary: str,
    task_ids: list[int] | None = None,
    annotation_ids: list[int] | None = None,
) -> AIBatch:
    """
    Create AIBatch + AIBatchChange rows from the current session diff and set
    session status to ready_for_review.
    """
    if session.status not in (AISession.Status.ACTIVE, AISession.Status.COMPILED):
        raise SessionWriteError(
            error="SESSION_NOT_ACTIVE",
            message=f"Session {session.id} must be active or compiled to finalize.",
            status_code=409,
        )
    if session.compile_status != AISession.CompileStatus.SUCCESS:
        raise SessionWriteError(
            error="COMPILE_REQUIRED",
            message="Session must compile successfully before it can be made ready for review.",
            status_code=409,
            suggestion="Run compilation and fix any errors before finalizing.",
        )
    if not session.staging_pdf_path:
        raise SessionWriteError(
            error="STAGING_PDF_REQUIRED",
            message="A preview PDF is required before the session can be made ready for review.",
            status_code=409,
            suggestion="Run compilation and fix any errors before finalizing.",
        )
    diff = generate_diff(session)

    project = session.project
    proc = _run_project_git(
        project,
        ["diff", "--name-status", "HEAD", session.branch_name],
        check=False,
    )

    batch = AIBatch.objects.create(session=session, summary=str(summary or "").strip() or "AI session")

    if task_ids:
        tasks = list(ProjectTask.objects.filter(project=project, id__in=task_ids))
        batch.tasks_completed.set(tasks)
    if annotation_ids:
        annotations = list(ProjectAnnotation.objects.filter(project=project, id__in=annotation_ids))
        batch.annotations_completed.set(annotations)

    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) < 2:
            continue
        status_code, filename = parts[0].strip(), parts[1].strip()
        if status_code.startswith("A"):
            change_type = AIBatchChange.ChangeType.CREATE
        elif status_code.startswith("D"):
            change_type = AIBatchChange.ChangeType.DELETE
        else:
            change_type = AIBatchChange.ChangeType.MODIFY

        file_diff_proc = _run_project_git(
            project,
            ["diff", "HEAD", session.branch_name, "--unified=3", "--", filename],
            check=False,
        )
        file_diff = (file_diff_proc.stdout or "").strip()
        lines_added = sum(1 for l in file_diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
        lines_removed = sum(1 for l in file_diff.splitlines() if l.startswith("-") and not l.startswith("---"))

        AIBatchChange.objects.create(
            batch=batch,
            filename=filename,
            change_type=change_type,
            diff_text=file_diff,
            lines_added=lines_added,
            lines_removed=lines_removed,
        )

    session.status = AISession.Status.READY_FOR_REVIEW
    session.save(update_fields=["status", "updated_at"])
    return batch


def accept_session(session: AISession, user=None) -> None:
    """
    Merge the session branch into the project HEAD, sync live project files,
    create a ProjectVersion per changed file, complete linked tasks, and clean up.
    """
    from projects.services import (
        create_project_version,
        pdf_file_path,
        project_dir as get_project_dir,
    )
    from projects.models import ProjectVersion

    if session.status not in (
        AISession.Status.ACTIVE,
        AISession.Status.COMPILED,
        AISession.Status.READY_FOR_REVIEW,
    ):
        raise SessionWriteError(
            error="SESSION_ALREADY_CLOSED",
            message=f"Session {session.id} cannot be accepted (status={session.status}).",
            status_code=409,
        )

    project = session.project

    # 1. Gather changed files before merge so we know what to version.
    changed_files = sorted(_get_session_changed_files(project, session))

    # Collect before-content from HEAD for each changed file (text files only).
    before_contents: dict[str, str] = {}
    for fname in changed_files:
        if Path(fname).suffix.lower() in _TEXT_EXTENSIONS_FOR_DIFF:
            proc = _run_project_git(project, ["show", f"HEAD:{fname}"], check=False)
            before_contents[fname] = proc.stdout if proc.returncode == 0 else ""
        else:
            before_contents[fname] = ""

    # 2. Merge session branch into HEAD with --no-ff so there is always a merge commit.
    merge_proc = _run_project_git(
        project,
        ["merge", "--no-ff", "--no-edit", session.branch_name],
        check=False,
    )
    if merge_proc.returncode != 0:
        merge_output = (merge_proc.stderr or merge_proc.stdout or "").strip()
        # Handle both "untracked files" and "local changes" that would be overwritten.
        if "would be overwritten by merge" in merge_output:
            _run_project_git(project, ["reset", "--hard", "HEAD"], check=False)
            _run_project_git(project, ["clean", "-fd"], check=False)
            merge_proc = _run_project_git(
                project,
                ["merge", "--no-ff", "--no-edit", session.branch_name],
                check=False,
            )
        if merge_proc.returncode != 0:
            raise RuntimeError(
                f"git merge failed: {(merge_proc.stderr or merge_proc.stdout or 'unknown error').strip()}"
            )

    # 3. Sync live project files from the git object store.
    proj_dir = get_project_dir(project)
    for fname in changed_files:
        ls_proc = _run_project_git(project, ["ls-tree", "HEAD", "--", fname], check=False)
        if ls_proc.returncode == 0 and (ls_proc.stdout or "").strip():
            target = proj_dir / fname
            target.parent.mkdir(parents=True, exist_ok=True)
            _run_project_git(project, ["checkout", "HEAD", "--", fname], check=False)
        else:
            # File was deleted in session
            target = proj_dir / fname
            if target.exists():
                target.unlink(missing_ok=True)

    # 3b. Auto-render any .puml files that were added or modified in this session.
    _render_puml_files(project, proj_dir, changed_files)

    if session.staging_pdf_path:
        staging_pdf = proj_dir / session.staging_pdf_path
        if staging_pdf.exists():
            live_pdf = pdf_file_path(project)
            live_pdf.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(staging_pdf, live_pdf)
            project.last_status = Project.CompileStatus.SUCCESS
            project.save(update_fields=["last_status", "updated_at"])

    # 4. Create a ProjectVersion record per changed file.
    batch_summary = ""
    try:
        batch = session.batch
        batch_summary = batch.summary
    except Exception:
        pass

    # Get the merge commit hash for the event payload.
    head_proc = _run_project_git(project, ["rev-parse", "HEAD"], check=False)
    merge_commit = (head_proc.stdout or "").strip()

    for fname in changed_files:
        if Path(fname).suffix.lower() in _TEXT_EXTENSIONS_FOR_DIFF:
            after_proc = _run_project_git(project, ["show", f"HEAD:{fname}"], check=False)
            after_content = after_proc.stdout if after_proc.returncode == 0 else ""
        else:
            after_content = ""
        create_project_version(
            project=project,
            actor=user,
            source="web",
            operation="ai_session_accept",
            target=fname,
            target_file=fname,
            category=ProjectVersion.Category.SESSION_ACCEPT,
            summary=batch_summary or f"AI session {session.id} accepted",
            before_content=before_contents.get(fname, ""),
            after_content=after_content,
            snapshot_kind=ProjectVersion.SnapshotKind.TEXT,
            event_payload={
                "session_id": session.id,
                "branch_name": session.branch_name,
                "merge_commit": merge_commit,
                "batch_summary": batch_summary,
            },
            is_revertible=True,
        )

    # 5. Complete linked tasks and annotations from the batch.
    try:
        batch = session.batch
        now = timezone.now()
        batch.tasks_completed.filter(
            status__in=(ProjectTask.Status.OPEN, ProjectTask.Status.IN_PROGRESS)
        ).update(status=ProjectTask.Status.DONE, completed_at=now, ai_session=session)
        batch.annotations_completed.filter(
            status__in=(ProjectAnnotation.Status.OPEN, ProjectAnnotation.Status.IN_PROGRESS)
        ).update(
            status=ProjectAnnotation.Status.DONE,
            resolved_at=now,
            resolved_by_session=session,
        )
    except Exception:
        pass

    # 6. Clean up worktree, branch, and session directory.
    _cleanup_session_files(project, session)

    session.status = AISession.Status.ACCEPTED
    session.accepted_at = timezone.now()
    session.save(update_fields=["status", "accepted_at", "updated_at"])
    try:
        proposal = session.change_proposal
        proposal.status = ChangeProposal.Status.ACCEPTED
        proposal.accepted_at = session.accepted_at
        proposal.user_visible_message = "Accepted"
        proposal.save(update_fields=["status", "accepted_at", "user_visible_message", "updated_at"])
    except Exception:
        pass


def discard_session(session: AISession) -> None:
    """
    Discard an active session: remove worktree, delete branch, delete session dir, unlock project.
    """
    if session.status in (AISession.Status.ACCEPTED, AISession.Status.DISCARDED, AISession.Status.EXPIRED):
        raise SessionWriteError(
            error="SESSION_ALREADY_CLOSED",
            message=f"Session {session.id} is already {session.status} and cannot be discarded again.",
            status_code=409,
        )
    _cleanup_session_files(session.project, session)
    session.status = AISession.Status.DISCARDED
    session.discarded_at = timezone.now()
    session.save(update_fields=["status", "discarded_at", "updated_at"])
    try:
        proposal = session.change_proposal
        if proposal.status not in (ChangeProposal.Status.ACCEPTED, ChangeProposal.Status.DISCARDED, ChangeProposal.Status.EXPIRED):
            proposal.status = ChangeProposal.Status.DISCARDED
            proposal.discarded_at = session.discarded_at
            proposal.user_visible_message = "Discarded"
            proposal.save(update_fields=["status", "discarded_at", "user_visible_message", "updated_at"])
    except Exception:
        pass


def expire_stale_sessions() -> int:
    """
    Find all sessions whose expires_at is in the past and clean them up.
    Returns the count of sessions that were expired.
    """
    now = timezone.now()
    overdue = list(
        AISession.objects.filter(
            status__in=AISession.locking_statuses(),
            expires_at__lt=now,
        ).select_related("project")
    )
    count = 0
    for session in overdue:
        try:
            _cleanup_session_files(session.project, session)
            session.status = AISession.Status.EXPIRED
            session.save(update_fields=["status", "updated_at"])
            count += 1
        except Exception:
            logger.exception("Failed to expire session %s", session.id)
    return count
