import shutil
import subprocess
import threading
import re
import difflib
from datetime import datetime, UTC
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from django.conf import settings

from .models import Project, ProjectVersion

COMPILE_SEMAPHORE = threading.BoundedSemaphore(value=3)
TEXT_EXTENSIONS = {".tex", ".sty", ".cls", ".bib", ".txt", ".md", ".csv", ".json", ".yaml", ".yml"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp"}
ALLOWED_UPLOAD_EXTENSIONS = TEXT_EXTENSIONS | IMAGE_EXTENSIONS | {".pdf"}
MAX_UPLOAD_FILE_SIZE = 8 * 1024 * 1024
LATEX_ARTIFACT_EXTENSIONS = {
    ".aux",
    ".log",
    ".out",
    ".toc",
    ".lof",
    ".lot",
    ".fls",
    ".fdb_latexmk",
    ".synctex.gz",
    ".xdv",
    ".bbl",
    ".blg",
    ".nav",
    ".snm",
    ".vrb",
}
SECTION_RE = re.compile(
    r"^\s*\\(?P<command>part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?\{(?P<title>[^}]*)\}",
    flags=re.MULTILINE,
)
SECTION_LEVELS = {
    "part": 1,
    "chapter": 1,
    "section": 2,
    "subsection": 3,
    "subsubsection": 4,
    "paragraph": 5,
    "subparagraph": 6,
}


@dataclass
class CompileResult:
    status: str
    log: str


@dataclass
class SectionChunk:
    index: int
    command: str
    level: int
    title: str
    start_line: int
    end_line: int
    start_char: int
    end_char: int
    content: str


def project_dir(project: Project) -> Path:
    return settings.MEDIA_ROOT / "projects" / str(project.owner_id) / str(project.id)


def tex_file_path(project: Project) -> Path:
    return project_dir(project) / "main.tex"


def pdf_file_path(project: Project) -> Path:
    return project_dir(project) / "main.pdf"


def log_file_path(project: Project) -> Path:
    return project_dir(project) / "main.log"


def ensure_project_dir(project: Project) -> Path:
    root = project_dir(project)
    root.mkdir(parents=True, exist_ok=True)
    return root


def initialize_main_tex(project: Project, content: str) -> None:
    ensure_project_dir(project)
    tex_file_path(project).write_text(content, encoding="utf-8")


def read_tex_content(project: Project) -> str:
    path = tex_file_path(project)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def write_tex_content(project: Project, content: str) -> None:
    ensure_project_dir(project)
    tex_file_path(project).write_text(content, encoding="utf-8")


def read_compile_log(project: Project) -> str:
    path = log_file_path(project)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def _safe_filename(filename: str) -> str:
    clean = Path(filename).name.strip()
    if not clean:
        raise ValueError("filename is required")
    if clean in {"main.tex", "main.pdf", "main.log"}:
        raise ValueError("cannot overwrite protected project file")
    if any(ch in clean for ch in ("/", "\\", "\x00")):
        raise ValueError("invalid filename")
    if clean.startswith("."):
        raise ValueError("hidden files are not allowed")
    ext = Path(clean).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise ValueError(f"unsupported file extension: {ext or '(none)'}")
    return clean


def project_asset_path(project: Project, filename: str) -> Path:
    clean = _safe_filename(filename)
    return project_dir(project) / clean


def list_project_assets(project: Project) -> list[dict[str, Any]]:
    root = ensure_project_dir(project)
    assets = []
    for path in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file():
            continue
        if path.name in {"main.tex", "main.pdf", "main.log"}:
            continue
        ext = path.suffix.lower()
        if path.name.startswith("."):
            continue
        if ext in LATEX_ARTIFACT_EXTENSIONS:
            continue
        assets.append(
            {
                "name": path.name,
                "size": path.stat().st_size,
                "updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(),
                "is_image": ext in IMAGE_EXTENSIONS,
                "is_text": ext in TEXT_EXTENSIONS,
                "extension": ext,
                "url": f"/api/projects/{project.id}/files/{quote(path.name)}",
            }
        )
    return assets


def save_project_asset(project: Project, filename: str, data: bytes) -> dict[str, Any]:
    if len(data) > MAX_UPLOAD_FILE_SIZE:
        raise ValueError(f"file exceeds {MAX_UPLOAD_FILE_SIZE // (1024 * 1024)}MB")
    path = project_asset_path(project, filename)
    ensure_project_dir(project)
    path.write_bytes(data)
    ext = path.suffix.lower()
    return {
        "name": path.name,
        "size": path.stat().st_size,
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(),
        "is_image": ext in IMAGE_EXTENSIONS,
        "is_text": ext in TEXT_EXTENSIONS,
        "extension": ext,
        "url": f"/api/projects/{project.id}/files/{quote(path.name)}",
    }


def _line_number_from_pos(content: str, pos: int) -> int:
    return content.count("\n", 0, pos) + 1


def split_tex_sections(content: str) -> list[SectionChunk]:
    lines = content.splitlines(keepends=True)
    total_lines = len(lines)
    matches = list(SECTION_RE.finditer(content))

    if not matches:
        return [
            SectionChunk(
                index=0,
                command="root",
                level=0,
                title="Преамбула / Загальний текст",
                start_line=1,
                end_line=max(1, total_lines),
                start_char=0,
                end_char=len(content),
                content=content,
            )
        ]

    chunks: list[SectionChunk] = []
    first_start = _line_number_from_pos(content, matches[0].start())
    pre_content = "".join(lines[: max(0, first_start - 1)])
    chunks.append(
        SectionChunk(
            index=0,
            command="root",
            level=0,
            title="Преамбула / До першого розділу",
            start_line=1,
            end_line=max(1, first_start - 1),
            start_char=0,
            end_char=matches[0].start(),
            content=pre_content,
        )
    )

    for idx, match in enumerate(matches, start=1):
        start_line = _line_number_from_pos(content, match.start())
        start_char = match.start()
        next_start_char = matches[idx].start() if idx < len(matches) else len(content)
        next_start_line = (
            _line_number_from_pos(content, matches[idx].start()) if idx < len(matches) else total_lines + 1
        )
        end_line = max(start_line, next_start_line - 1)
        end_char = max(start_char, next_start_char)
        section_content = "".join(lines[start_line - 1 : end_line])
        command = match.group("command")
        title = match.group("title").strip() or command.capitalize()
        chunks.append(
            SectionChunk(
                index=idx,
                command=command,
                level=SECTION_LEVELS.get(command, 2),
                title=title,
                start_line=start_line,
                end_line=end_line,
                start_char=start_char,
                end_char=end_char,
                content=section_content,
            )
        )
    return chunks


def list_tex_sections(project: Project) -> list[dict[str, Any]]:
    chunks = split_tex_sections(read_tex_content(project))
    return [
        {
            "index": c.index,
            "command": c.command,
            "level": c.level,
            "title": c.title,
            "start_line": c.start_line,
            "end_line": c.end_line,
            "start_char": c.start_char,
            "end_char": c.end_char,
            "line_count": max(0, c.end_line - c.start_line + 1),
            "char_count": max(0, c.end_char - c.start_char),
        }
        for c in chunks
    ]


def get_tex_section(project: Project, section_index: int) -> dict[str, Any]:
    chunks = split_tex_sections(read_tex_content(project))
    for chunk in chunks:
        if chunk.index == section_index:
            return {
                "index": chunk.index,
                "command": chunk.command,
                "level": chunk.level,
                "title": chunk.title,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "start_char": chunk.start_char,
                "end_char": chunk.end_char,
                "content": chunk.content,
            }
    raise ValueError("section not found")


def update_tex_section(project: Project, section_index: int, new_content: str) -> dict[str, Any]:
    if not isinstance(new_content, str):
        raise ValueError("content must be a string")

    source = read_tex_content(project)
    chunks = split_tex_sections(source)
    target = next((c for c in chunks if c.index == section_index), None)
    if not target:
        raise ValueError("section not found")

    lines = source.splitlines(keepends=True)
    start_idx = max(0, target.start_line - 1)
    end_idx = max(start_idx, target.end_line)
    replacement = new_content
    if replacement and not replacement.endswith("\n"):
        replacement = f"{replacement}\n"
    replacement_lines = replacement.splitlines(keepends=True)
    updated = "".join(lines[:start_idx] + replacement_lines + lines[end_idx:])

    if is_tex_too_large(updated):
        raise ValueError("File exceeds 1MB")

    write_tex_content(project, updated)
    return get_tex_section(project, section_index)


def insert_text_at_position(project: Project, position: int, text: str) -> dict[str, Any]:
    if not isinstance(position, int):
        raise ValueError("position must be an integer")
    if not isinstance(text, str):
        raise ValueError("text must be a string")

    source = read_tex_content(project)
    if position < 0 or position > len(source):
        raise ValueError("position is out of bounds")

    updated = source[:position] + text + source[position:]
    if is_tex_too_large(updated):
        raise ValueError("File exceeds 1MB")

    write_tex_content(project, updated)
    return {
        "position": position,
        "inserted_length": len(text),
        "new_length": len(updated),
    }


def _resolve_text_file_path(project: Project, file_name: str) -> Path:
    name = (file_name or "main.tex").strip()
    if not name:
        name = "main.tex"
    if name == "main.tex":
        return tex_file_path(project)
    if Path(name).name != name or any(ch in name for ch in ("/", "\\", "\x00")):
        raise ValueError("invalid file_name")
    path = project_dir(project) / name
    if not path.exists() or not path.is_file():
        raise ValueError("file not found")
    if path.suffix.lower() not in TEXT_EXTENSIONS:
        raise ValueError("file is not a text file")
    return path


def _read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def read_project_window(
    project: Project,
    *,
    file_name: str = "main.tex",
    start_line: int | None = None,
    end_line: int | None = None,
    start_char: int | None = None,
    end_char: int | None = None,
) -> dict[str, Any]:
    path = _resolve_text_file_path(project, file_name)
    content = _read_text_file(path)
    lines = content.splitlines()

    has_line_window = start_line is not None or end_line is not None
    has_char_window = start_char is not None or end_char is not None
    if has_line_window and has_char_window:
        raise ValueError("use either line window or char window, not both")

    if has_char_window:
        s_char = 0 if start_char is None else int(start_char)
        e_char = len(content) if end_char is None else int(end_char)
        if s_char < 0 or e_char < 0:
            raise ValueError("char offsets must be non-negative")
        if e_char < s_char:
            raise ValueError("end_char must be >= start_char")
        if s_char > len(content) or e_char > len(content):
            raise ValueError("char offsets out of bounds")
        chunk = content[s_char:e_char]
        return {
            "file_name": path.name,
            "mode": "chars",
            "start_char": s_char,
            "end_char": e_char,
            "start_line": _line_number_from_pos(content, s_char),
            "end_line": _line_number_from_pos(content, e_char),
            "total_lines": len(lines),
            "total_chars": len(content),
            "content": chunk,
        }

    s_line = 1 if start_line is None else int(start_line)
    e_line = min(len(lines), s_line + 199) if end_line is None else int(end_line)
    if s_line < 1 or e_line < 1:
        raise ValueError("line numbers are 1-based and must be positive")
    if e_line < s_line:
        raise ValueError("end_line must be >= start_line")
    if s_line > max(1, len(lines)):
        raise ValueError("start_line out of bounds")
    e_line = min(e_line, len(lines))
    chunk = "\n".join(lines[s_line - 1 : e_line])
    if content.endswith("\n") and e_line == len(lines):
        chunk += "\n"
    return {
        "file_name": path.name,
        "mode": "lines",
        "start_line": s_line,
        "end_line": e_line,
        "start_char": None,
        "end_char": None,
        "total_lines": len(lines),
        "total_chars": len(content),
        "content": chunk,
    }


def write_project_window(
    project: Project,
    *,
    replacement: str,
    file_name: str = "main.tex",
    start_line: int | None = None,
    end_line: int | None = None,
    start_char: int | None = None,
    end_char: int | None = None,
) -> dict[str, Any]:
    if not isinstance(replacement, str):
        raise ValueError("replacement must be a string")

    path = _resolve_text_file_path(project, file_name)
    source = _read_text_file(path)
    lines_keepends = source.splitlines(keepends=True)
    lines = source.splitlines()

    has_line_window = start_line is not None or end_line is not None
    has_char_window = start_char is not None or end_char is not None
    if has_line_window and has_char_window:
        raise ValueError("use either line window or char window, not both")

    if has_char_window:
        s_char = 0 if start_char is None else int(start_char)
        e_char = len(source) if end_char is None else int(end_char)
        if s_char < 0 or e_char < 0:
            raise ValueError("char offsets must be non-negative")
        if e_char < s_char:
            raise ValueError("end_char must be >= start_char")
        if s_char > len(source) or e_char > len(source):
            raise ValueError("char offsets out of bounds")

        updated = source[:s_char] + replacement + source[e_char:]
        if is_tex_too_large(updated):
            raise ValueError("File exceeds 1MB")
        path.write_text(updated, encoding="utf-8")
        return {
            "file_name": path.name,
            "mode": "chars",
            "start_char": s_char,
            "end_char": e_char,
            "replaced_chars": e_char - s_char,
            "inserted_chars": len(replacement),
            "new_total_chars": len(updated),
            "new_total_lines": len(updated.splitlines()),
        }

    s_line = 1 if start_line is None else int(start_line)
    e_line = min(len(lines), s_line + 199) if end_line is None else int(end_line)
    if s_line < 1 or e_line < 1:
        raise ValueError("line numbers are 1-based and must be positive")
    if e_line < s_line:
        raise ValueError("end_line must be >= start_line")
    if s_line > max(1, len(lines)):
        raise ValueError("start_line out of bounds")
    e_line = min(e_line, len(lines))

    start_idx = s_line - 1
    end_idx = e_line
    replacement_text = replacement
    if replacement_text and not replacement_text.endswith("\n"):
        replacement_text = f"{replacement_text}\n"
    replacement_lines = replacement_text.splitlines(keepends=True)
    updated = "".join(lines_keepends[:start_idx] + replacement_lines + lines_keepends[end_idx:])
    if is_tex_too_large(updated):
        raise ValueError("File exceeds 1MB")
    path.write_text(updated, encoding="utf-8")

    return {
        "file_name": path.name,
        "mode": "lines",
        "start_line": s_line,
        "end_line": e_line,
        "replaced_lines": max(0, e_line - s_line + 1),
        "inserted_lines": len(replacement_text.splitlines()),
        "new_total_chars": len(updated),
        "new_total_lines": len(updated.splitlines()),
    }


def search_project_content(
    project: Project,
    *,
    query: str,
    is_regex: bool = False,
    ignore_case: bool = True,
    max_results: int = 200,
    include_main: bool = True,
    include_assets: bool = True,
) -> dict[str, Any]:
    term = (query or "").strip()
    if not term:
        raise ValueError("query is required")
    max_results = max(1, min(int(max_results), 2000))

    flags = re.IGNORECASE if ignore_case else 0
    pattern = term if is_regex else re.escape(term)
    try:
        rx = re.compile(pattern, flags=flags)
    except re.error as exc:
        raise ValueError(f"invalid regex: {exc}") from exc

    files: list[Path] = []
    if include_main:
        files.append(tex_file_path(project))
    if include_assets:
        for asset in list_project_assets(project):
            if asset.get("is_text"):
                files.append(project_dir(project) / str(asset["name"]))

    matches: list[dict[str, Any]] = []
    files_scanned = 0
    for path in files:
        if not path.exists() or not path.is_file():
            continue
        files_scanned += 1
        text = _read_text_file(path)
        for line_no, line in enumerate(text.splitlines(), start=1):
            for m in rx.finditer(line):
                matches.append(
                    {
                        "file_name": path.name,
                        "line": line_no,
                        "column": m.start() + 1,
                        "line_text": line,
                        "match_text": m.group(0),
                    }
                )
                if len(matches) >= max_results:
                    return {
                        "query": term,
                        "is_regex": bool(is_regex),
                        "ignore_case": bool(ignore_case),
                        "max_results": max_results,
                        "truncated": True,
                        "files_scanned": files_scanned,
                        "matches": matches,
                        "total_matches": len(matches),
                    }

    return {
        "query": term,
        "is_regex": bool(is_regex),
        "ignore_case": bool(ignore_case),
        "max_results": max_results,
        "truncated": False,
        "files_scanned": files_scanned,
        "matches": matches,
        "total_matches": len(matches),
    }


def create_project_version(
    *,
    project: Project,
    actor,
    source: str,
    operation: str,
    target: str,
    summary: str,
    before_content: str,
    after_content: str,
) -> ProjectVersion:
    return ProjectVersion.objects.create(
        project=project,
        actor=actor,
        source=source,
        operation=operation,
        target=target,
        summary=summary.strip(),
        before_content=before_content,
        after_content=after_content,
    )


def list_project_versions(
    project: Project,
    *,
    limit: int = 40,
    before_id: int | None = None,
) -> dict[str, Any]:
    safe_limit = max(1, min(int(limit), 120))
    qs = ProjectVersion.objects.filter(project=project)
    if before_id is not None:
        qs = qs.filter(id__lt=int(before_id))
    rows = list(
        qs.select_related("actor")
        .order_by("-created_at", "-id")[: safe_limit + 1]
    )
    has_more = len(rows) > safe_limit
    page = rows[:safe_limit]
    versions = [
        {
            "id": v.id,
            "source": v.source,
            "operation": v.operation,
            "target": v.target,
            "summary": v.summary,
            "actor": v.actor.username if v.actor else None,
            "created_at": v.created_at.isoformat(),
        }
        for v in page
    ]
    next_before_id = versions[-1]["id"] if has_more and versions else None
    return {
        "versions": versions,
        "has_more": has_more,
        "next_before_id": next_before_id,
    }


def get_project_version(project: Project, version_id: int) -> ProjectVersion:
    return ProjectVersion.objects.get(project=project, id=version_id)


def build_version_diff(version: ProjectVersion, context_lines: int = 2) -> str:
    before = version.before_content.splitlines(keepends=True)
    after = version.after_content.splitlines(keepends=True)
    target = (version.target or "main.tex").split(":", 1)[0]
    diff = difflib.unified_diff(
        before,
        after,
        fromfile=f"a/{target}",
        tofile=f"b/{target}",
        lineterm="",
        n=context_lines,
    )
    text = "\n".join(diff)
    return text or "(no changes)"


def rollback_to_version(project: Project, version: ProjectVersion) -> None:
    target = (version.target or "").split(":", 1)[0]
    if target != "main.tex":
        raise ValueError("Rollback is supported only for main.tex versions")
    write_tex_content(project, version.after_content)


def delete_project_files(project: Project) -> None:
    root = project_dir(project)
    if root.exists() and root.is_dir():
        shutil.rmtree(root)


def compile_project(project: Project) -> CompileResult:
    workdir = ensure_project_dir(project)
    input_file = tex_file_path(project)

    if not input_file.exists():
        return CompileResult(status=Project.CompileStatus.ERROR, log="main.tex not found")

    image = getattr(settings, "LATEX_DOCKER_IMAGE", "latex-ua:latest")
    timeout = int(getattr(settings, "LATEX_TIMEOUT_SECONDS", 60))
    strict_errors = bool(getattr(settings, "LATEX_STRICT_ERRORS", False))
    host_project_root = str(getattr(settings, "HOST_PROJECT_ROOT", "")).strip()

    # When running inside Docker with host docker socket mounted, docker daemon expects
    # host-absolute paths in -v source, not container-internal (/app/...) paths.
    docker_mount_source = workdir
    if host_project_root:
        docker_mount_source = Path(host_project_root) / "media" / "projects" / str(project.owner_id) / str(project.id)
        docker_mount_source.mkdir(parents=True, exist_ok=True)

    latex_args = [
        "lualatex",
        "-interaction=nonstopmode",
    ]
    if strict_errors:
        latex_args.append("-halt-on-error")
    latex_args.append("main.tex")

    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--memory=600m",
        "--cpus=1.0",
        "-v",
        f"{docker_mount_source}:/workspace:rw",
        "-w",
        "/workspace",
        image,
        *latex_args,
    ]

    existing_pdf = pdf_file_path(project)
    had_pdf_before = existing_pdf.exists()
    pdf_mtime_before = existing_pdf.stat().st_mtime_ns if had_pdf_before else None

    acquired = COMPILE_SEMAPHORE.acquire(timeout=timeout)
    if not acquired:
        return CompileResult(status=Project.CompileStatus.ERROR, log="Compilation queue timeout")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        log_text = (proc.stdout or "") + "\n" + (proc.stderr or "")
        log_file_path(project).write_text(log_text, encoding="utf-8", errors="ignore")

        pdf_exists_after = existing_pdf.exists()
        pdf_mtime_after = existing_pdf.stat().st_mtime_ns if pdf_exists_after else None
        pdf_was_updated = (
            pdf_exists_after
            and (
                not had_pdf_before
                or pdf_mtime_before is None
                or pdf_mtime_after != pdf_mtime_before
            )
        )

        # Success when LaTeX exited cleanly with a PDF, or produced/updated PDF despite non-fatal issues.
        if pdf_exists_after and (proc.returncode == 0 or pdf_was_updated):
            return CompileResult(status=Project.CompileStatus.SUCCESS, log=log_text)

        return CompileResult(status=Project.CompileStatus.ERROR, log=log_text or "Compilation failed")
    except subprocess.TimeoutExpired:
        log_text = f"Compilation timed out after {timeout} seconds"
        log_file_path(project).write_text(log_text, encoding="utf-8")
        return CompileResult(status=Project.CompileStatus.ERROR, log=log_text)
    except FileNotFoundError:
        log_text = "Docker is not installed or unavailable in PATH"
        log_file_path(project).write_text(log_text, encoding="utf-8")
        return CompileResult(status=Project.CompileStatus.ERROR, log=log_text)
    except Exception as exc:  # pragma: no cover
        log_text = f"Unexpected error: {exc}"
        log_file_path(project).write_text(log_text, encoding="utf-8", errors="ignore")
        return CompileResult(status=Project.CompileStatus.ERROR, log=log_text)
    finally:
        COMPILE_SEMAPHORE.release()


def is_tex_too_large(content: str) -> bool:
    limit = int(getattr(settings, "MAX_TEX_FILE_SIZE", 1024 * 1024))
    return len(content.encode("utf-8")) > limit


def has_pdf(project: Project) -> bool:
    return pdf_file_path(project).exists()


def pdf_relative_url(project: Project) -> str:
    # Keep PDF access behind authenticated API endpoint instead of public /media path.
    return f"/api/projects/{project.id}/pdf/"


def pdf_version(project: Project) -> int | None:
    path = pdf_file_path(project)
    if not path.exists():
        return None
    # Nanoseconds avoid collisions when multiple compiles finish within the same second.
    return int(path.stat().st_mtime_ns)
