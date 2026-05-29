import io
import logging
import shutil
import subprocess
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings

from SmartTeX.markup import MarkupType, source_filename_for_markup

from .models import Template

logger = logging.getLogger(__name__)

COMPILE_SEMAPHORE = threading.BoundedSemaphore(value=2)
TEMPLATE_TEXT_EXTENSIONS = {".tex", ".typ", ".sty", ".cls", ".bib", ".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".csl"}
TEMPLATE_ASSET_EXTENSIONS = TEMPLATE_TEXT_EXTENSIONS | {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp", ".pdf"}
MAX_TEMPLATE_ZIP_PREVIEW_BYTES = int(getattr(settings, "MAX_TEMPLATE_ZIP_PREVIEW_BYTES", 64 * 1024 * 1024))

SMARTTEX_CONTEXT_PREFIX = ".smarttex/context/"
_CONTEXT_TEXT_EXTENSIONS = {".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".tex", ".typ", ".bib", ".csl"}



@dataclass
class TemplateCompileResult:
    status: str   # "success" | "error"
    log: str


def _compiler_network_args(markup_type: str) -> list[str]:
    if markup_type == MarkupType.TYPST:
        network = str(getattr(settings, "TYPST_DOCKER_NETWORK", "bridge")).strip() or "bridge"
    else:
        network = "none"
    return ["--network", network]


def extract_smarttex_context_from_zip(template: Template, context_root: "Path") -> list[dict]:
    """
    Extract .smarttex/context/ files from the template ZIP into *context_root*.

    Returns a list of dicts with ``filename`` and ``size`` for each written file.
    Silently returns [] when the ZIP is absent, has no context files, or on any error.
    """
    if not template.zip_file:
        return []
    try:
        template.zip_file.open("rb")
        with zipfile.ZipFile(template.zip_file) as zf:
            entries = [i for i in zf.infolist() if not i.is_dir()]
            strip_prefix = _strip_common_zip_prefix(entries)
            created: list[dict] = []
            for info in entries:
                raw_name = info.filename
                if strip_prefix and raw_name.startswith(strip_prefix):
                    raw_name = raw_name[len(strip_prefix):]
                # Must be under .smarttex/context/
                if not raw_name.startswith(SMARTTEX_CONTEXT_PREFIX):
                    continue
                rel_name = raw_name[len(SMARTTEX_CONTEXT_PREFIX):]
                rel_name = rel_name.strip("/")
                if not rel_name:
                    continue
                parts = Path(rel_name).parts
                if not parts or any(p in {"", ".", ".."} for p in parts):
                    continue
                # No nested hidden dirs
                if any(p.startswith(".") for p in parts):
                    continue
                ext = Path(parts[-1]).suffix.lower()
                if ext not in _CONTEXT_TEXT_EXTENSIONS:
                    continue

                data = zf.read(info.filename)
                root = context_root.resolve()
                target = (root / Path(rel_name)).resolve()
                if root != target and root not in target.parents:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    target.write_text(data.decode("utf-8"), encoding="utf-8")
                except UnicodeDecodeError:
                    target.write_bytes(data)
                created.append({"filename": rel_name, "size": len(data)})
            return created
    except Exception:
        logger.debug("Could not extract .smarttex/context from template ZIP", extra={"template_id": template.id})
        return []


def template_preview_dir(template: Template) -> Path:
    return settings.MEDIA_ROOT / "templates" / str(template.id)


def normalize_template_main_file(template: Template) -> str:
    raw = str(getattr(template, "main_file", "") or "").strip().replace("\\", "/")
    default = source_filename_for_markup(template.markup_type)
    if not raw:
        return default
    path = Path(raw)
    parts = path.parts
    if path.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        return default
    suffix = Path(parts[-1]).suffix.lower()
    if template.markup_type == MarkupType.TYPST and suffix != ".typ":
        return default
    if template.markup_type != MarkupType.TYPST and suffix != ".tex":
        return default
    return "/".join(parts)


def template_source_path(template: Template) -> Path:
    return template_preview_dir(template) / normalize_template_main_file(template)


def _clean_template_preview_dir(workdir: Path) -> None:
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)


def _template_zip_entries(template: Template) -> list[zipfile.ZipInfo]:
    if not template.zip_file:
        return []
    try:
        template.zip_file.open("rb")
        with zipfile.ZipFile(template.zip_file) as zf:
            return [i for i in zf.infolist() if not i.is_dir()]
    except (OSError, zipfile.BadZipFile):
        return []


def _strip_common_zip_prefix(entries: list[zipfile.ZipInfo]) -> str:
    top_dirs = {i.filename.split("/")[0] for i in entries if "/" in i.filename}
    if len(top_dirs) != 1:
        return ""
    candidate = top_dirs.pop() + "/"
    if entries and all(i.filename.startswith(candidate) for i in entries):
        return candidate
    return ""


def _safe_zip_relative_name(raw_name: str, strip_prefix: str = "") -> str | None:
    name = raw_name
    if strip_prefix and name.startswith(strip_prefix):
        name = name[len(strip_prefix):]
    name = name.strip("/")
    if not name:
        return None
    parts = Path(name).parts
    if not parts or "__MACOSX" in parts or any(p.startswith(".") for p in parts):
        return None
    if any(p in {"", ".", ".."} for p in parts):
        return None
    ext = Path(parts[-1]).suffix.lower()
    if ext not in TEMPLATE_ASSET_EXTENSIONS:
        return None
    return "/".join(parts)


def template_zip_file_list(template: Template, *, limit: int | None = None) -> list[dict[str, Any]]:
    entries = _template_zip_entries(template)
    strip_prefix = _strip_common_zip_prefix(entries)
    files: list[dict[str, Any]] = []
    for info in entries:
        name = _safe_zip_relative_name(info.filename, strip_prefix)
        if not name:
            continue
        files.append({"name": name, "size": info.file_size, "is_main": name == normalize_template_main_file(template)})
    files.sort(key=lambda item: (not item["is_main"], item["name"].lower()))
    if limit is not None:
        return files[: max(0, int(limit))]
    return files


def template_zip_summary(template: Template) -> dict[str, Any]:
    files = template_zip_file_list(template)
    source_name = normalize_template_main_file(template)
    has_source = any(f["name"] == source_name for f in files)
    return {
        "has_zip": bool(template.zip_file),
        "file_count": len(files),
        "files": files[:80],
        "main_file": source_name,
        "main_file_exists": has_source or bool(template.content),
        "more_count": max(0, len(files) - 80),
    }


def extract_template_zip_to_preview(template: Template, workdir: Path) -> list[dict[str, Any]]:
    if not template.zip_file:
        return []
    try:
        template.zip_file.open("rb")
        zf = zipfile.ZipFile(template.zip_file)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Невалідний ZIP-файл: {exc}") from exc

    root = workdir.resolve()
    created: list[dict[str, Any]] = []
    total_size = 0
    with zf:
        entries = [i for i in zf.infolist() if not i.is_dir()]
        strip_prefix = _strip_common_zip_prefix(entries)
        for info in entries:
            name = _safe_zip_relative_name(info.filename, strip_prefix)
            if not name:
                continue
            data = zf.read(info.filename)
            total_size += len(data)
            if total_size > MAX_TEMPLATE_ZIP_PREVIEW_BYTES:
                raise ValueError("Template ZIP preview exceeds size limit")
            target = (root / Path(name)).resolve()
            if root != target and root not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if Path(name).suffix.lower() in TEMPLATE_TEXT_EXTENSIONS:
                try:
                    target.write_text(data.decode("utf-8"), encoding="utf-8")
                except UnicodeDecodeError:
                    target.write_bytes(data)
            else:
                target.write_bytes(data)
            created.append({"name": name, "size": len(data)})
    return created


def template_pdf_path(template: Template) -> Path:
    return template_preview_dir(template) / "preview.pdf"


def template_compile_log_path(template: Template) -> Path:
    return template_preview_dir(template) / "preview.log"


def read_template_compile_log(template: Template) -> str:
    path = template_compile_log_path(template)
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def has_template_pdf(template: Template) -> bool:
    return template_pdf_path(template).exists()


def template_pdf_url(template: Template) -> str:
    # Expose preview only via authenticated Django view.
    return f"/templates/{template.id}/pdf/"


def template_pdf_version(template: Template) -> int | None:
    path = template_pdf_path(template)
    if not path.exists():
        return None
    return int(path.stat().st_mtime_ns)


def _template_compile_debug_header(
    *,
    template: Template,
    workdir: Path,
    docker_mount_source: Path,
    source_name: str,
    source_path: Path,
    extracted: list[dict[str, Any]],
    image: str,
    cmd: list[str],
    timeout: int,
) -> str:
    visible_files = []
    try:
        visible_files = sorted(
            str(path.relative_to(workdir)).replace("\\", "/")
            for path in workdir.rglob("*")
            if path.is_file()
        )[:80]
    except OSError:
        visible_files = []

    lines = [
        "=== SmartTeX template compile debug ===",
        f"template_id={template.id}",
        f"title={template.title}",
        f"markup_type={template.markup_type}",
        f"configured_main_file={getattr(template, 'main_file', '') or '<empty>'}",
        f"normalized_main_file={source_name}",
        f"zip_file={getattr(template.zip_file, 'name', '') or '<none>'}",
        f"inline_content_bytes={len((template.content or '').encode('utf-8'))}",
        f"workdir={workdir}",
        f"docker_mount_source={docker_mount_source}",
        f"source_path={source_path}",
        f"source_exists={source_path.exists()}",
        f"source_size={source_path.stat().st_size if source_path.exists() else 0}",
        f"preview_pdf_path={template_pdf_path(template)}",
        f"image={image}",
        f"timeout={timeout}",
        f"extracted_files={len(extracted)}",
        "extracted_sample=" + ", ".join(f["name"] for f in extracted[:20]),
        "workdir_files_sample=" + ", ".join(visible_files[:40]),
        "cmd=" + " ".join(str(part) for part in cmd),
        "=== compiler output ===",
    ]
    return "\n".join(lines) + "\n"


def _write_template_compile_log(template: Template, log_text: str) -> None:
    try:
        path = template_compile_log_path(template)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(log_text, encoding="utf-8", errors="ignore")
    except OSError:
        logger.exception("Could not write template compile log", extra={"template_id": template.id})


def compile_template_preview(template: Template) -> TemplateCompileResult:
    workdir = template_preview_dir(template)
    _clean_template_preview_dir(workdir)

    try:
        extracted = extract_template_zip_to_preview(template, workdir)
    except ValueError as exc:
        log_text = f"=== SmartTeX template compile debug ===\ntemplate_id={template.id}\nzip_extract_error={exc}\n"
        _write_template_compile_log(template, log_text)
        logger.warning("Template ZIP extraction failed", extra={"template_id": template.id, "error": str(exc)})
        return TemplateCompileResult(status="error", log=log_text)

    source_name = normalize_template_main_file(template)
    source_path = workdir / source_name
    source_path.parent.mkdir(parents=True, exist_ok=True)

    if template.content:
        source_path.write_text(template.content, encoding="utf-8")
    elif not source_path.exists():
        fallback = source_filename_for_markup(template.markup_type)
        if source_name != fallback and (workdir / fallback).exists():
            logger.warning(
                "Configured template main file was not found; falling back to default",
                extra={"template_id": template.id, "configured_main_file": source_name, "fallback": fallback},
            )
            source_name = fallback
            source_path = workdir / fallback
        else:
            # Keep an empty source only so the compiler log clearly shows what failed.
            source_path.write_text("", encoding="utf-8")
            logger.warning(
                "Template main file was not found after ZIP extraction",
                extra={"template_id": template.id, "main_file": source_name, "workdir": str(workdir)},
            )

    host_project_root = str(getattr(settings, "HOST_PROJECT_ROOT", "")).strip()
    docker_mount_source = workdir
    if host_project_root:
        docker_mount_source = Path(host_project_root) / "media" / "templates" / str(template.id)
        docker_mount_source.mkdir(parents=True, exist_ok=True)

    if template.markup_type == MarkupType.TYPST:
        image = getattr(settings, "TYPST_DOCKER_IMAGE", "ghcr.io/typst/typst:latest")
        timeout = int(getattr(settings, "TYPST_TIMEOUT_SECONDS", 60))
        compiler_args = ["compile", "--root", "/workspace", source_name, "preview.pdf"]
    else:
        image = getattr(settings, "LATEX_DOCKER_IMAGE", "latex-ua:latest")
        timeout = int(getattr(settings, "LATEX_TIMEOUT_SECONDS", 60))
        compiler_args = [
            "lualatex",
            "-interaction=nonstopmode",
            "-jobname=preview",
            source_name,
        ]

    cmd = [
        "docker", "run", "--rm",
        *_compiler_network_args(template.markup_type),
        "--memory=600m", "--cpus=1.0",
        "-v", f"{docker_mount_source}:/workspace:rw",
        "-w", "/workspace",
        image,
        *compiler_args,
    ]
    debug_header = _template_compile_debug_header(
        template=template,
        workdir=workdir,
        docker_mount_source=docker_mount_source,
        source_name=source_name,
        source_path=source_path,
        extracted=extracted,
        image=str(image),
        cmd=cmd,
        timeout=timeout,
    )

    logger.info(
        "Starting template preview compile",
        extra={"template_id": template.id, "main_file": source_name, "workdir": str(workdir)},
    )

    acquired = COMPILE_SEMAPHORE.acquire(timeout=timeout)
    if not acquired:
        log_text = debug_header + "Compilation queue timeout"
        _write_template_compile_log(template, log_text)
        return TemplateCompileResult(status="error", log=log_text)

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
        compiler_output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        pdf_exists = template_pdf_path(template).exists()
        footer = (
            "\n=== SmartTeX template compile result ===\n"
            f"returncode={proc.returncode}\n"
            f"pdf_exists={pdf_exists}\n"
            f"pdf_size={template_pdf_path(template).stat().st_size if pdf_exists else 0}\n"
        )
        log_text = debug_header + compiler_output + footer
        _write_template_compile_log(template, log_text)

        if pdf_exists and (proc.returncode == 0 or pdf_exists):
            logger.info("Template preview compile succeeded", extra={"template_id": template.id, "main_file": source_name})
            return TemplateCompileResult(status="success", log=log_text)
        logger.warning(
            "Template preview compile failed",
            extra={"template_id": template.id, "main_file": source_name, "returncode": proc.returncode, "pdf_exists": pdf_exists},
        )
        return TemplateCompileResult(status="error", log=log_text or "Compilation failed")
    except subprocess.TimeoutExpired:
        log_text = debug_header + f"Timed out after {timeout}s"
        _write_template_compile_log(template, log_text)
        logger.warning("Template preview compile timed out", extra={"template_id": template.id, "timeout": timeout})
        return TemplateCompileResult(status="error", log=log_text)
    except FileNotFoundError:
        log_text = debug_header + "Docker not found"
        _write_template_compile_log(template, log_text)
        logger.exception("Docker not found for template preview compile", extra={"template_id": template.id})
        return TemplateCompileResult(status="error", log=log_text)
    except Exception as exc:
        log_text = debug_header + f"Unexpected error: {exc}"
        _write_template_compile_log(template, log_text)
        logger.exception("Unexpected template preview compile failure", extra={"template_id": template.id})
        return TemplateCompileResult(status="error", log=log_text)
    finally:
        COMPILE_SEMAPHORE.release()
