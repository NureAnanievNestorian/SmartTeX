import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

from SmartTeX.markup import MarkupType, source_filename_for_markup

from .models import Template

COMPILE_SEMAPHORE = threading.BoundedSemaphore(value=2)


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


def template_preview_dir(template: Template) -> Path:
    return settings.MEDIA_ROOT / "templates" / str(template.id)


def template_source_path(template: Template) -> Path:
    return template_preview_dir(template) / source_filename_for_markup(template.markup_type)


def template_pdf_path(template: Template) -> Path:
    return template_preview_dir(template) / "preview.pdf"


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


def compile_template_preview(template: Template) -> TemplateCompileResult:
    workdir = template_preview_dir(template)
    workdir.mkdir(parents=True, exist_ok=True)

    source_path = template_source_path(template)
    source_path.write_text(template.content, encoding="utf-8")

    host_project_root = str(getattr(settings, "HOST_PROJECT_ROOT", "")).strip()
    docker_mount_source = workdir
    if host_project_root:
        docker_mount_source = Path(host_project_root) / "media" / "templates" / str(template.id)
        docker_mount_source.mkdir(parents=True, exist_ok=True)

    if template.markup_type == MarkupType.TYPST:
        image = getattr(settings, "TYPST_DOCKER_IMAGE", "ghcr.io/typst/typst:latest")
        timeout = int(getattr(settings, "TYPST_TIMEOUT_SECONDS", 60))
        compiler_args = ["compile", "main.typ", "preview.pdf"]
    else:
        image = getattr(settings, "LATEX_DOCKER_IMAGE", "latex-ua:latest")
        timeout = int(getattr(settings, "LATEX_TIMEOUT_SECONDS", 60))
        compiler_args = [
            "lualatex",
            "-interaction=nonstopmode",
            "-jobname=preview",
            "main.tex",
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

    acquired = COMPILE_SEMAPHORE.acquire(timeout=timeout)
    if not acquired:
        return TemplateCompileResult(status="error", log="Compilation queue timeout")

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
        log_text = (proc.stdout or "") + "\n" + (proc.stderr or "")
        pdf_exists = template_pdf_path(template).exists()

        if pdf_exists and (proc.returncode == 0 or pdf_exists):
            return TemplateCompileResult(status="success", log=log_text)
        return TemplateCompileResult(status="error", log=log_text or "Compilation failed")
    except subprocess.TimeoutExpired:
        return TemplateCompileResult(status="error", log=f"Timed out after {timeout}s")
    except FileNotFoundError:
        return TemplateCompileResult(status="error", log="Docker not found")
    except Exception as exc:
        return TemplateCompileResult(status="error", log=f"Unexpected error: {exc}")
    finally:
        COMPILE_SEMAPHORE.release()
