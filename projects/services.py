import shutil
import subprocess
import threading
import re
import difflib
import base64
import io
import zipfile
from datetime import datetime, UTC
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from django.conf import settings

from SmartTeX.markup import MarkupType, source_filename_for_markup

from .models import Project, ProjectVersion

COMPILE_SEMAPHORE = threading.BoundedSemaphore(value=3)
TEXT_EXTENSIONS = {".tex", ".typ", ".sty", ".cls", ".bib", ".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".csl"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp"}
ALLOWED_UPLOAD_EXTENSIONS = TEXT_EXTENSIONS | IMAGE_EXTENSIONS | {".pdf"}
MAX_PROJECT_FILES_TOTAL_SIZE = 20 * 1024 * 1024
PROTECTED_FILENAMES = {"main.tex", "main.typ", "main.pdf", "main.log"}
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
    r"^\s*\\(?P<command>newappendix|appendix|appendices|part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?"
    r"(?:(?:\{(?P<appendix_label>[^}]*)\}\{(?P<appendix_title>[^}]*)\})|(?:\{(?P<title>[^}]*)\}))?",
    flags=re.MULTILINE,
)
SECTION_LEVELS = {
    "newappendix": 1,
    "appendix": 1,
    "appendices": 1,
    "part": 1,
    "chapter": 1,
    "section": 2,
    "subsection": 3,
    "subsubsection": 4,
    "paragraph": 5,
    "subparagraph": 6,
}
TYPST_HEADING_RE = re.compile(r"^(?P<marks>={1,6})\s+(?P<title>.+?)\s*$", flags=re.MULTILINE)


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


def main_source_filename(project: Project) -> str:
    return source_filename_for_markup(project.markup_type)


def source_file_path(project: Project) -> Path:
    return project_dir(project) / main_source_filename(project)


def pdf_file_path(project: Project) -> Path:
    return project_dir(project) / "main.pdf"


def project_pdf_download_name(project: Project) -> str:
    raw = (project.title or "").strip()
    if not raw:
        raw = "document"
    # Keep Unicode filename but strip filesystem/HTTP-problematic characters.
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", raw).strip(" .")
    if not cleaned:
        cleaned = "document"
    return f"{cleaned}.pdf"


def log_file_path(project: Project) -> Path:
    return project_dir(project) / "main.log"


def ensure_project_dir(project: Project) -> Path:
    root = project_dir(project)
    root.mkdir(parents=True, exist_ok=True)
    return root


def initialize_main_source(project: Project, content: str) -> None:
    ensure_project_dir(project)
    source_file_path(project).write_text(content, encoding="utf-8")


def read_source_content(project: Project) -> str:
    path = source_file_path(project)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def write_source_content(project: Project, content: str) -> None:
    ensure_project_dir(project)
    source_file_path(project).write_text(content, encoding="utf-8")


def read_compile_log(project: Project) -> str:
    path = log_file_path(project)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def _safe_file_path(project: Project, filename: str) -> Path:
    name = str(filename or "").strip()
    if not name:
        raise ValueError("filename is required")
    if "\x00" in name:
        raise ValueError("invalid filename")

    raw_path = Path(name)
    parts = raw_path.parts
    if not parts:
        raise ValueError("filename is required")
    if raw_path.is_absolute():
        raise ValueError("absolute paths not allowed")
    if any(part in {".", ".."} for part in parts):
        raise ValueError("path traversal not allowed")

    final = parts[-1]
    if final.startswith("."):
        raise ValueError("hidden files not allowed")

    ext = Path(final).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise ValueError(f"unsupported file extension: {ext or '(none)'}")

    root = ensure_project_dir(project).resolve()
    resolved = (root / raw_path).resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError("path escapes project directory")
    if resolved.name in PROTECTED_FILENAMES:
        raise ValueError("cannot overwrite protected project file")
    return resolved


def _safe_directory_path(project: Project, directory: str) -> Path:
    name = str(directory or "").strip().rstrip("/\\")
    if not name:
        raise ValueError("directory path is required")
    if "\x00" in name:
        raise ValueError("invalid directory path")

    raw_path = Path(name)
    parts = raw_path.parts
    if not parts:
        raise ValueError("directory path is required")
    if raw_path.is_absolute():
        raise ValueError("absolute paths not allowed")
    if any(part in {".", ".."} for part in parts):
        raise ValueError("path traversal not allowed")
    if any(part.startswith(".") for part in parts):
        raise ValueError("hidden folders not allowed")

    root = ensure_project_dir(project).resolve()
    resolved = (root / raw_path).resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError("path escapes project directory")
    if resolved == root:
        raise ValueError("cannot use project root as a custom folder")
    if resolved.name in PROTECTED_FILENAMES:
        raise ValueError("protected names cannot be used for folders")
    return resolved


def _safe_entry_path(project: Project, name: str) -> Path:
    raw = str(name or "").strip().rstrip("/\\")
    if not raw:
        raise ValueError("path is required")
    try:
        return _safe_file_path(project, raw)
    except ValueError as exc:
        if str(exc).startswith("unsupported file extension:"):
            return _safe_directory_path(project, raw)
        raise


def project_asset_path(project: Project, filename: str) -> Path:
    return _safe_entry_path(project, filename)


def _relative_project_path(project: Project, path: Path) -> str:
    root = ensure_project_dir(project).resolve()
    return str(path.resolve().relative_to(root)).replace("\\", "/")


def _asset_payload(project: Project, path: Path) -> dict[str, Any]:
    rel_path = _relative_project_path(project, path)
    is_dir = path.is_dir()
    ext = path.suffix.lower()
    return {
        "name": rel_path,
        "path": rel_path,
        "size": None if is_dir else path.stat().st_size,
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(),
        "is_dir": is_dir,
        "is_image": False if is_dir else ext in IMAGE_EXTENSIONS,
        "is_text": False if is_dir else ext in TEXT_EXTENSIONS,
        "extension": "" if is_dir else ext,
        "url": None if is_dir else f"/api/projects/{project.id}/files/{quote(rel_path, safe='')}",
    }


def _is_system_artifact_file(path: Path) -> bool:
    name = path.name.lower()
    ext = path.suffix.lower()
    full_ext = "".join(path.suffixes).lower()
    if ext in LATEX_ARTIFACT_EXTENSIONS or full_ext in LATEX_ARTIFACT_EXTENSIONS:
        return True
    if name.startswith("main.synctex"):
        return True
    if ".synctex(" in name or name.endswith(".synctex"):
        return True
    return False


def list_project_assets(project: Project) -> list[dict[str, Any]]:
    root = ensure_project_dir(project)
    assets = []
    for path in sorted(root.rglob("*"), key=lambda p: str(p.relative_to(root)).lower()):
        if path.name.startswith("."):
            continue
        if path.is_dir():
            assets.append(_asset_payload(project, path))
            continue
        if path.parent == root and path.name in PROTECTED_FILENAMES:
            continue
        if _is_system_artifact_file(path):
            continue
        assets.append(_asset_payload(project, path))
    return assets


def save_project_asset(project: Project, filename: str, data: bytes) -> dict[str, Any]:
    path = project_asset_path(project, filename)
    ensure_project_dir(project)
    existing_size = path.stat().st_size if path.exists() and path.is_file() else 0

    total_size = 0
    source_path = source_file_path(project)
    if source_path.exists() and source_path.is_file():
        total_size += source_path.stat().st_size
    for asset in list_project_assets(project):
        total_size += int(asset.get("size") or 0)

    projected_total = total_size - existing_size + len(data)
    if projected_total > MAX_PROJECT_FILES_TOTAL_SIZE:
        raise ValueError(
            f"project files total size exceeds {MAX_PROJECT_FILES_TOTAL_SIZE // (1024 * 1024)}MB"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return _asset_payload(project, path)


def create_project_text_file(project: Project, filename: str, content: str) -> dict[str, Any]:
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    if is_source_too_large(content):
        raise ValueError("File exceeds 1MB")

    path = project_asset_path(project, filename)
    ext = path.suffix.lower()
    if ext not in TEXT_EXTENSIONS:
        raise ValueError(f"not a text file extension: {ext or '(none)'}")
    if path.exists():
        raise ValueError("file already exists; use write_project_window to edit")

    total_size = 0
    source_path = source_file_path(project)
    if source_path.exists() and source_path.is_file():
        total_size += source_path.stat().st_size
    for asset in list_project_assets(project):
        total_size += int(asset.get("size") or 0)
    content_bytes = content.encode("utf-8")
    if total_size + len(content_bytes) > MAX_PROJECT_FILES_TOTAL_SIZE:
        raise ValueError(
            f"project files total size exceeds {MAX_PROJECT_FILES_TOTAL_SIZE // (1024 * 1024)}MB"
        )

    ensure_project_dir(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content_bytes)
    return _asset_payload(project, path)


def create_project_directory(project: Project, directory: str) -> dict[str, Any]:
    path = _safe_directory_path(project, directory)
    if path.exists():
        raise ValueError("folder already exists")
    ensure_project_dir(project)
    path.mkdir(parents=True, exist_ok=False)
    return _asset_payload(project, path)


def read_project_asset_content(
    project: Project,
    filename: str,
    *,
    include_text: bool = False,
) -> dict[str, Any]:
    path = project_asset_path(project, filename)
    if not path.exists() or not path.is_file():
        raise ValueError("file not found")
    data = path.read_bytes()
    ext = path.suffix.lower()
    payload: dict[str, Any] = {
        "name": _relative_project_path(project, path),
        "path": _relative_project_path(project, path),
        "size": len(data),
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(),
        "is_image": ext in IMAGE_EXTENSIONS,
        "is_text": ext in TEXT_EXTENSIONS,
        "extension": ext,
        "url": f"/api/projects/{project.id}/files/{quote(_relative_project_path(project, path), safe='')}",
        "content_base64": base64.b64encode(data).decode("ascii"),
    }
    if include_text and ext in TEXT_EXTENSIONS:
        payload["text_content"] = data.decode("utf-8", errors="ignore")
    return payload


def write_project_asset_text(project: Project, filename: str, content: str) -> dict[str, Any]:
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    if is_source_too_large(content):
        raise ValueError("File exceeds 1MB")

    path = project_asset_path(project, filename)
    if not path.exists() or not path.is_file():
        raise ValueError("file not found")

    ext = path.suffix.lower()
    if ext not in TEXT_EXTENSIONS:
        raise ValueError("file is not a text file")

    path.write_text(content, encoding="utf-8")
    payload = _asset_payload(project, path)
    payload["text_content"] = content
    return payload


def extract_project_zip(project: Project, zip_bytes: bytes) -> list[dict[str, Any]]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Невалідний ZIP-файл: {exc}") from exc

    root = ensure_project_dir(project).resolve()

    total_size = 0
    src = source_file_path(project)
    if src.exists():
        total_size += src.stat().st_size
    for a in list_project_assets(project):
        total_size += int(a.get("size") or 0)

    # Strip common top-level directory shared by all entries (e.g. "project/...").
    all_entries = [i for i in zf.infolist() if not i.is_dir()]
    top_dirs = {i.filename.split("/")[0] for i in zf.infolist() if "/" in i.filename}
    strip_prefix = ""
    if len(top_dirs) == 1:
        candidate = top_dirs.pop() + "/"
        if all_entries and all(i.filename.startswith(candidate) for i in all_entries):
            strip_prefix = candidate

    created: list[dict[str, Any]] = []
    with zf:
        for info in all_entries:
            name = info.filename
            if strip_prefix and name.startswith(strip_prefix):
                name = name[len(strip_prefix):]
            name = name.strip("/")
            if not name:
                continue
            parts = Path(name).parts
            if not parts:
                continue
            if "__MACOSX" in parts or any(p.startswith(".") for p in parts):
                continue
            if any(p in {".", ".."} for p in parts):
                continue

            leaf = parts[-1]
            ext = Path(leaf).suffix.lower()
            if ext not in ALLOWED_UPLOAD_EXTENSIONS:
                continue
            if len(parts) == 1 and leaf in PROTECTED_FILENAMES:
                continue

            data = zf.read(info.filename)
            if total_size + len(data) > MAX_PROJECT_FILES_TOTAL_SIZE:
                raise ValueError(
                    f"Вміст ZIP перевищує ліміт проєкту {MAX_PROJECT_FILES_TOTAL_SIZE // (1024 * 1024)} МБ"
                )

            target = (root / Path(name)).resolve()
            if root != target and root not in target.parents:
                continue
            if target.parent == root and target.name in PROTECTED_FILENAMES:
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            if ext in TEXT_EXTENSIONS:
                try:
                    target.write_text(data.decode("utf-8"), encoding="utf-8")
                except UnicodeDecodeError:
                    target.write_bytes(data)
            else:
                target.write_bytes(data)

            total_size += len(data)
            created.append(_asset_payload(project, target))

    return created


def rename_project_asset(project: Project, filename: str, new_filename: str) -> dict[str, Any]:
    old_path = project_asset_path(project, filename)
    if not old_path.exists():
        raise ValueError("file not found")

    new_path = _safe_directory_path(project, new_filename) if old_path.is_dir() else _safe_file_path(project, new_filename)
    if _relative_project_path(project, old_path) == _relative_project_path(project, new_path):
        raise ValueError("new filename must be different")
    if old_path.is_file() and old_path.suffix.lower() != new_path.suffix.lower():
        raise ValueError("file extension cannot be changed")
    if new_path.exists():
        raise ValueError("target path already exists")

    old_name = _relative_project_path(project, old_path)
    new_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.rename(new_path)
    return {"old_name": old_name, **_asset_payload(project, new_path)}


def delete_project_asset(project: Project, filename: str) -> dict[str, Any]:
    path = project_asset_path(project, filename)
    if not path.exists():
        raise ValueError("file not found")

    payload = _asset_payload(project, path)
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return {**payload, "deleted": True}


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
        raw_title = (match.group("title") or "").strip()
        appendix_label = (match.group("appendix_label") or "").strip()
        appendix_title = (match.group("appendix_title") or "").strip()
        if command == "newappendix":
            if appendix_label and appendix_title:
                title = f"Додаток {appendix_label}: {appendix_title}"
            elif appendix_title:
                title = f"Додаток: {appendix_title}"
            elif appendix_label:
                title = f"Додаток {appendix_label}"
            else:
                title = "Додаток"
        elif raw_title:
            title = raw_title
        elif command in {"appendix", "appendices"}:
            title = "Додатки"
        else:
            title = command.capitalize()
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


def split_typst_sections(content: str) -> list[SectionChunk]:
    lines = content.splitlines(keepends=True)
    total_lines = len(lines)
    matches = list(TYPST_HEADING_RE.finditer(content))

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
        marks = match.group("marks") or "="
        level = min(len(marks), 6)
        start_line = _line_number_from_pos(content, match.start())
        start_char = match.start()
        next_start_char = matches[idx].start() if idx < len(matches) else len(content)
        next_start_line = (
            _line_number_from_pos(content, matches[idx].start()) if idx < len(matches) else total_lines + 1
        )
        end_line = max(start_line, next_start_line - 1)
        end_char = max(start_char, next_start_char)
        section_content = "".join(lines[start_line - 1 : end_line])
        title = (match.group("title") or "").strip() or f"Heading {level}"
        chunks.append(
            SectionChunk(
                index=idx,
                command=f"heading{level}",
                level=level,
                title=title,
                start_line=start_line,
                end_line=end_line,
                start_char=start_char,
                end_char=end_char,
                content=section_content,
            )
        )
    return chunks


def _split_source_sections(project: Project, content: str) -> list[SectionChunk]:
    if project.markup_type == MarkupType.TYPST:
        return split_typst_sections(content)
    return split_tex_sections(content)


def _section_payload(chunk: SectionChunk, *, include_content: bool = False) -> dict[str, Any]:
    payload = {
        "index": chunk.index,
        "command": chunk.command,
        "level": chunk.level,
        "title": chunk.title,
        "start_line": chunk.start_line,
        "end_line": chunk.end_line,
        "start_char": chunk.start_char,
        "end_char": chunk.end_char,
    }
    if include_content:
        payload["content"] = chunk.content
    else:
        payload["line_count"] = max(0, chunk.end_line - chunk.start_line + 1)
        payload["char_count"] = max(0, chunk.end_char - chunk.start_char)
    return payload


def list_source_sections(project: Project) -> list[dict[str, Any]]:
    chunks = _split_source_sections(project, read_source_content(project))
    return [
        _section_payload(c, include_content=False)
        for c in chunks
    ]


def get_source_section(project: Project, section_index: int) -> dict[str, Any]:
    chunks = _split_source_sections(project, read_source_content(project))
    for chunk in chunks:
        if chunk.index == section_index:
            return _section_payload(chunk, include_content=True)
    raise ValueError("section not found")


def update_source_section(project: Project, section_index: int, new_content: str) -> dict[str, Any]:
    if not isinstance(new_content, str):
        raise ValueError("content must be a string")

    source = read_source_content(project)
    chunks = _split_source_sections(project, source)
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

    if is_source_too_large(updated):
        raise ValueError("File exceeds 1MB")

    write_source_content(project, updated)
    return get_source_section(project, section_index)


def insert_text_at_position(project: Project, position: int, text: str) -> dict[str, Any]:
    if not isinstance(position, int):
        raise ValueError("position must be an integer")
    if not isinstance(text, str):
        raise ValueError("text must be a string")

    source = read_source_content(project)
    if position < 0 or position > len(source):
        raise ValueError("position is out of bounds")

    updated = source[:position] + text + source[position:]
    if is_source_too_large(updated):
        raise ValueError("File exceeds 1MB")

    write_source_content(project, updated)
    return {
        "position": position,
        "inserted_length": len(text),
        "new_length": len(updated),
    }


def _resolve_text_file_path(project: Project, file_name: str) -> Path:
    default_name = main_source_filename(project)
    name = (file_name or default_name).strip()
    if not name:
        name = default_name
    if name == default_name:
        return source_file_path(project)
    path = _safe_file_path(project, name)
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
    file_name: str | None = None,
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
    file_name: str | None = None,
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
        if is_source_too_large(updated):
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
    if is_source_too_large(updated):
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
        files.append(source_file_path(project))
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
    from django.db import transaction
    with transaction.atomic():
        last = (
            ProjectVersion.objects.filter(project=project)
            .order_by("-number")
            .values_list("number", flat=True)
            .first()
        )
        next_number = (last or 0) + 1
        return ProjectVersion.objects.create(
            project=project,
            number=next_number,
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
            "number": v.number,
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
    target = (version.target or main_source_filename(version.project)).split(":", 1)[0]
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
    if target not in {"main.tex", "main.typ"}:
        raise ValueError("Rollback is supported only for source file versions")
    write_source_content(project, version.after_content)


def delete_project_files(project: Project) -> None:
    root = project_dir(project)
    if root.exists() and root.is_dir():
        shutil.rmtree(root)


def _compiler_network_args(markup_type: str) -> list[str]:
    if markup_type == MarkupType.TYPST:
        network = str(getattr(settings, "TYPST_DOCKER_NETWORK", "bridge")).strip() or "bridge"
    else:
        network = "none"
    return ["--network", network]


def compile_project(project: Project) -> CompileResult:
    workdir = ensure_project_dir(project)
    input_file = source_file_path(project)

    if not input_file.exists():
        return CompileResult(status=Project.CompileStatus.ERROR, log=f"{input_file.name} not found")

    host_project_root = str(getattr(settings, "HOST_PROJECT_ROOT", "")).strip()

    # When running inside Docker with host docker socket mounted, docker daemon expects
    # host-absolute paths in -v source, not container-internal (/app/...) paths.
    docker_mount_source = workdir
    if host_project_root:
        docker_mount_source = Path(host_project_root) / "media" / "projects" / str(project.owner_id) / str(project.id)
        docker_mount_source.mkdir(parents=True, exist_ok=True)

    use_native_typst = (
        project.markup_type == MarkupType.TYPST
        and bool(getattr(settings, "TYPST_USE_NATIVE", False))
    )

    if use_native_typst:
        timeout = int(getattr(settings, "TYPST_TIMEOUT_SECONDS", 60))
        typst_bin = str(getattr(settings, "TYPST_BINARY", "typst")).strip() or "typst"
        cmd = [typst_bin, "compile", "main.typ", "main.pdf"]
        run_kwargs: dict = {"cwd": str(workdir)}
    elif project.markup_type == MarkupType.TYPST:
        image = getattr(settings, "TYPST_DOCKER_IMAGE", "ghcr.io/typst/typst:latest")
        timeout = int(getattr(settings, "TYPST_TIMEOUT_SECONDS", 60))
        compiler_args = ["compile", "main.typ", "main.pdf"]
        cmd = [
            "docker", "run", "--rm",
            *_compiler_network_args(project.markup_type),
            "--memory=600m", "--cpus=1.0",
            "-v", f"{docker_mount_source}:/workspace:rw",
            "-w", "/workspace",
            image,
            *compiler_args,
        ]
        run_kwargs = {}
    else:
        image = getattr(settings, "LATEX_DOCKER_IMAGE", "latex-ua:latest")
        timeout = int(getattr(settings, "LATEX_TIMEOUT_SECONDS", 60))
        strict_errors = bool(getattr(settings, "LATEX_STRICT_ERRORS", False))
        compiler_args = [
            "lualatex",
            "-interaction=nonstopmode",
            "-synctex=1",
        ]
        if strict_errors:
            compiler_args.append("-halt-on-error")
        compiler_args.append("main.tex")
        cmd = [
            "docker", "run", "--rm",
            *_compiler_network_args(project.markup_type),
            "--memory=600m", "--cpus=1.0",
            "-v", f"{docker_mount_source}:/workspace:rw",
            "-w", "/workspace",
            image,
            *compiler_args,
        ]
        run_kwargs = {}

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
            **run_kwargs,
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

        # Success when compiler exited cleanly with a PDF, or produced/updated PDF despite non-fatal issues.
        if pdf_exists_after and (proc.returncode == 0 or pdf_was_updated):
            return CompileResult(status=Project.CompileStatus.SUCCESS, log=log_text)

        return CompileResult(status=Project.CompileStatus.ERROR, log=log_text or "Compilation failed")
    except subprocess.TimeoutExpired:
        log_text = f"Compilation timed out after {timeout} seconds"
        log_file_path(project).write_text(log_text, encoding="utf-8")
        return CompileResult(status=Project.CompileStatus.ERROR, log=log_text)
    except FileNotFoundError:
        log_text = (
            "typst binary not found in PATH" if use_native_typst
            else "Docker is not installed or unavailable in PATH"
        )
        log_file_path(project).write_text(log_text, encoding="utf-8")
        return CompileResult(status=Project.CompileStatus.ERROR, log=log_text)
    except Exception as exc:  # pragma: no cover
        log_text = f"Unexpected error: {exc}"
        log_file_path(project).write_text(log_text, encoding="utf-8", errors="ignore")
        return CompileResult(status=Project.CompileStatus.ERROR, log=log_text)
    finally:
        COMPILE_SEMAPHORE.release()


def is_source_too_large(content: str) -> bool:
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


def _docker_mount_source(project: Project) -> Path:
    workdir = ensure_project_dir(project)
    host_project_root = str(getattr(settings, "HOST_PROJECT_ROOT", "")).strip()
    if host_project_root:
        host_path = Path(host_project_root) / "media" / "projects" / str(project.owner_id) / str(project.id)
        host_path.mkdir(parents=True, exist_ok=True)
        return host_path
    return workdir


def render_pdf_page_image(
    project: Project,
    *,
    page: int = 1,
    scale: float = 1.5,
    image_format: str = "png",
) -> dict[str, Any]:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover
        raise ValueError("PDF rendering dependency is not installed") from exc
    pdf_path = pdf_file_path(project)
    if not pdf_path.exists():
        raise ValueError("PDF not found")
    if page < 1:
        raise ValueError("page must be >= 1")

    fmt = (image_format or "png").strip().upper()
    if fmt not in {"PNG", "JPEG", "WEBP"}:
        raise ValueError("image_format must be one of: png, jpeg, webp")
    safe_scale = max(0.5, min(float(scale), 4.0))

    doc = pdfium.PdfDocument(str(pdf_path))
    page_count = len(doc)
    if page > page_count:
        raise ValueError(f"page out of bounds (1..{page_count})")

    pdf_page = doc[page - 1]
    bitmap = pdf_page.render(scale=safe_scale)
    image = bitmap.to_pil()
    if fmt in {"JPEG", "WEBP"} and image.mode not in {"RGB", "L"}:
        image = image.convert("RGB")

    buf = io.BytesIO()
    save_kwargs: dict[str, Any] = {}
    if fmt == "JPEG":
        save_kwargs["quality"] = 88
        save_kwargs["optimize"] = True
    image.save(buf, format=fmt, **save_kwargs)
    image_bytes = buf.getvalue()
    mime = "image/png" if fmt == "PNG" else ("image/jpeg" if fmt == "JPEG" else "image/webp")

    return {
        "page": page,
        "page_count": page_count,
        "scale": safe_scale,
        "image_format": fmt.lower(),
        "mime_type": mime,
        "width": int(image.width),
        "height": int(image.height),
        "image_base64": base64.b64encode(image_bytes).decode("ascii"),
    }


def get_project_pdf_page_count(project: Project) -> dict[str, Any]:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover
        raise ValueError("PDF rendering dependency is not installed") from exc
    pdf_path = pdf_file_path(project)
    if not pdf_path.exists():
        raise ValueError("PDF not found; compile project first")

    doc = pdfium.PdfDocument(str(pdf_path))
    page_count = len(doc)
    try:
        file_size = pdf_path.stat().st_size
        updated_at = datetime.fromtimestamp(pdf_path.stat().st_mtime, tz=UTC).isoformat()
    except OSError:
        file_size = None
        updated_at = None
    return {
        "file_name": pdf_path.name,
        "page_count": int(page_count),
        "file_size": file_size,
        "updated_at": updated_at,
    }


def synctex_line_to_pdf(
    project: Project,
    *,
    line: int,
    file_name: str | None = None,
    column: int = 1,
) -> dict[str, Any]:
    if project.markup_type == MarkupType.TYPST:
        raise ValueError("Source mapping is not available for Typst projects")
    if line < 1:
        raise ValueError("line must be >= 1")
    if column < 1:
        raise ValueError("column must be >= 1")

    tex_path = _resolve_text_file_path(project, file_name or main_source_filename(project))
    pdf_path = pdf_file_path(project)
    if not pdf_path.exists():
        raise ValueError("PDF not found; compile project first")

    mount_source = _docker_mount_source(project)
    image = getattr(settings, "LATEX_DOCKER_IMAGE", "latex-ua:latest")
    query = f"{line}:{column}:{tex_path.name}"
    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--memory=300m",
        "--cpus=0.5",
        "-v",
        f"{mount_source}:/workspace:rw",
        "-w",
        "/workspace",
        image,
        "synctex",
        "view",
        "-i",
        query,
        "-o",
        "main.pdf",
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()

    matches: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in out.splitlines():
        line_text = raw.strip()
        if line_text.startswith("Page:"):
            if current:
                matches.append(current)
            try:
                page_no = int(line_text.split(":", 1)[1].strip())
            except Exception:
                page_no = 0
            current = {"page": page_no}
            continue
        if current and ":" in line_text:
            key, value = line_text.split(":", 1)
            key = key.strip().lower()
            value = value.strip()
            if key in {"x", "y", "h", "v"}:
                try:
                    current[key] = float(value)
                except ValueError:
                    current[key] = value
    if current:
        matches.append(current)

    pages = sorted({int(m.get("page", 0)) for m in matches if int(m.get("page", 0)) > 0})
    if not pages and proc.returncode != 0:
        tail = "\n".join(out.splitlines()[-20:]).strip()
        raise ValueError(tail or "SyncTeX command failed")

    return {
        "file_name": tex_path.name,
        "line": int(line),
        "column": int(column),
        "pages": pages,
        "matches": matches,
    }


def synctex_pdf_to_line(
    project: Project,
    *,
    page: int,
    x: float,
    y: float,
) -> dict[str, Any]:
    """Reverse SyncTeX: given a position in the PDF, return the LaTeX source location."""
    if project.markup_type == MarkupType.TYPST:
        raise ValueError("Source mapping is not available for Typst projects")
    if page < 1:
        raise ValueError("page must be >= 1")

    pdf_path = pdf_file_path(project)
    if not pdf_path.exists():
        raise ValueError("PDF not found; compile project first")

    mount_source = _docker_mount_source(project)
    image = getattr(settings, "LATEX_DOCKER_IMAGE", "latex-ua:latest")
    query = f"{page}:{x:.6f}:{y:.6f}:main.pdf"
    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--memory=300m",
        "--cpus=0.5",
        "-v",
        f"{mount_source}:/workspace:rw",
        "-w",
        "/workspace",
        image,
        "synctex",
        "edit",
        "-o",
        query,
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()

    result: dict[str, Any] = {"page": int(page), "x": x, "y": y, "file": None, "line": None, "column": None}
    for raw in out.splitlines():
        line_text = raw.strip()
        if ":" not in line_text:
            continue
        key, value = line_text.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "input":
            # Strip /workspace/ prefix Docker adds
            name = value.replace("/workspace/", "").strip()
            result["file"] = name
        elif key == "line":
            try:
                result["line"] = int(value)
            except ValueError:
                pass
        elif key == "column":
            try:
                result["column"] = int(value)
            except ValueError:
                pass

    if result["line"] is None and proc.returncode != 0:
        tail = "\n".join(out.splitlines()[-10:]).strip()
        raise ValueError(tail or "SyncTeX edit command failed")

    result["_debug"] = {"query": query, "raw": out}
    return result
