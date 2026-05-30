from __future__ import annotations

import base64
import hashlib
import json
import zlib
from pathlib import Path
from typing import Any

import httpx
from django.conf import settings

from projects.pre_compile import PreCompileResult, PreCompileJob, register

_HASHES_FILE = ".smarttex/plantuml_hashes.json"

_B64_TO_PUML = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_",
)


def encode_plantuml(source: str) -> str:
    compressed = zlib.compress(source.encode("utf-8"), level=9)
    return base64.b64encode(compressed).decode("ascii").translate(_B64_TO_PUML)


def _plantuml_url() -> str:
    return str(getattr(settings, "PLANTUML_URL", "http://plantuml:8080")).rstrip("/")


def render_plantuml_svg(source: str, timeout: int = 15) -> bytes:
    encoded = encode_plantuml(source)
    url = f"{_plantuml_url()}/svg/{encoded}"
    response = httpx.get(url, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    return response.content


def _load_hashes(workdir: Path) -> dict[str, str]:
    hashes_path = workdir / _HASHES_FILE
    if hashes_path.exists():
        try:
            return json.loads(hashes_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_hashes(workdir: Path, hashes: dict[str, str]) -> None:
    hashes_path = workdir / _HASHES_FILE
    hashes_path.parent.mkdir(parents=True, exist_ok=True)
    hashes_path.write_text(json.dumps(hashes, indent=2, sort_keys=True), encoding="utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@register
class PlantUmlJob(PreCompileJob):
    name = "plantuml"

    def run(self, project: Any, workdir: Path) -> PreCompileResult:
        puml_files = sorted(workdir.rglob("*.puml"))
        if not puml_files:
            return PreCompileResult(job=self.name, success=True)

        hashes = _load_hashes(workdir)
        processed = 0
        skipped = 0
        errors: list[str] = []
        hashes_dirty = False

        for puml_path in puml_files:
            rel = str(puml_path.relative_to(workdir)).replace("\\", "/")
            content_bytes = puml_path.read_bytes()
            current_hash = _sha256(content_bytes)
            if hashes.get(rel) == current_hash:
                skipped += 1
                continue
            try:
                svg_bytes = render_plantuml_svg(content_bytes.decode("utf-8", errors="replace"))
            except Exception as exc:
                errors.append(f"{rel}: {exc}")
                continue
            svg_path = puml_path.with_suffix(".svg")
            svg_path.write_bytes(svg_bytes)
            hashes[rel] = current_hash
            hashes_dirty = True
            processed += 1

        if hashes_dirty:
            _save_hashes(workdir, hashes)

        return PreCompileResult(
            job=self.name,
            success=not errors,
            files_processed=processed,
            files_skipped=skipped,
            errors=errors,
        )
