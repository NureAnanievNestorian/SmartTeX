"""Deterministic file discovery and role classification for the navigation index."""
from __future__ import annotations

import hashlib
import re
import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from django.conf import settings

from projects.models import Project
from projects.services import project_dir, main_source_filename

from ..models import FileRole


INDEX_TEXT_EXTENSIONS = {
    ".tex", ".typ", ".sty", ".cls", ".bib", ".csl",
    ".yml", ".yaml", ".toml", ".md", ".txt", ".json", ".csv",
}

SKIP_DIR_PARTS = {".git", ".smarttex", ".smarttex-git", "__pycache__", "node_modules"}
SKIP_DIR_PREFIXES = (".smarttex-git",)

NAV_MAX_FILE_BYTES = int(getattr(settings, "NAV_MAX_FILE_BYTES", 512 * 1024))
NAV_MAX_INDEXED_FILES = int(getattr(settings, "NAV_MAX_INDEXED_FILES", 5000))


@dataclass
class DiscoveredFile:
    filename: str
    absolute_path: Path
    byte_size: int
    line_count: int
    content_hash: str
    excluded: bool = False
    exclusion_reason: str = ""
    is_binary: bool = False


@dataclass
class DiscoveryResult:
    files: list[DiscoveredFile] = field(default_factory=list)
    skipped: list[DiscoveredFile] = field(default_factory=list)
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def all_filenames(self) -> list[str]:
        return [f.filename for f in self.files]


def _canon_rel_path(value: str | None) -> str:
    if not value:
        return ""
    raw = str(value).replace("\\", "/")
    normalized = posixpath.normpath(raw)
    if normalized == ".":
        return ""
    return normalized.lstrip("./")


def _is_skipped_dir(parts: Iterable[str]) -> bool:
    for part in parts:
        if part in SKIP_DIR_PARTS:
            return True
        for prefix in SKIP_DIR_PREFIXES:
            if part.startswith(prefix):
                return True
    return False


def _looks_binary(sample: bytes) -> bool:
    if b"\x00" in sample:
        return True
    if not sample:
        return False
    printable = sum(1 for b in sample if b in (9, 10, 13) or 32 <= b < 127)
    return (printable / max(1, len(sample))) < 0.7


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def discover_project_files(project: Project) -> DiscoveryResult:
    root = project_dir(project)
    result = DiscoveryResult()
    if not root.exists():
        return result

    main_file = _canon_rel_path(main_source_filename(project))
    candidates: list[Path] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if _is_skipped_dir(rel_parts[:-1]):
            continue
        if rel_parts[-1].startswith(".") and rel_parts[-1] != ".gitignore":
            continue
        candidates.append(path)

    for path in candidates:
        rel = _canon_rel_path(path.relative_to(root).as_posix())

        try:
            stat = path.stat()
        except OSError:
            continue

        size = stat.st_size
        suffix = path.suffix.lower()
        is_main = rel == main_file

        if suffix not in INDEX_TEXT_EXTENSIONS and not is_main:
            result.skipped.append(
                DiscoveredFile(
                    filename=rel,
                    absolute_path=path,
                    byte_size=size,
                    line_count=0,
                    content_hash="",
                    excluded=True,
                    exclusion_reason="unsupported_extension",
                )
            )
            result.skip_reasons["unsupported_extension"] = result.skip_reasons.get("unsupported_extension", 0) + 1
            continue

        if size > NAV_MAX_FILE_BYTES and not is_main:
            result.skipped.append(
                DiscoveredFile(
                    filename=rel,
                    absolute_path=path,
                    byte_size=size,
                    line_count=0,
                    content_hash="",
                    excluded=True,
                    exclusion_reason="oversize",
                )
            )
            result.skip_reasons["oversize"] = result.skip_reasons.get("oversize", 0) + 1
            continue

        try:
            data = path.read_bytes()
        except OSError:
            continue

        if _looks_binary(data[:4096]):
            result.skipped.append(
                DiscoveredFile(
                    filename=rel,
                    absolute_path=path,
                    byte_size=size,
                    line_count=0,
                    content_hash="",
                    excluded=True,
                    exclusion_reason="binary",
                    is_binary=True,
                )
            )
            result.skip_reasons["binary"] = result.skip_reasons.get("binary", 0) + 1
            continue

        text = data.decode("utf-8", errors="replace")
        line_count = text.count("\n") + (0 if text.endswith("\n") or not text else 1)

        result.files.append(
            DiscoveredFile(
                filename=rel,
                absolute_path=path,
                byte_size=size,
                line_count=line_count,
                content_hash=_hash_bytes(data),
            )
        )

    if len(result.files) > NAV_MAX_INDEXED_FILES:
        kept = result.files[:NAV_MAX_INDEXED_FILES]
        dropped = result.files[NAV_MAX_INDEXED_FILES:]
        for item in dropped:
            item.excluded = True
            item.exclusion_reason = "project_too_large"
            result.skipped.append(item)
        result.skip_reasons["project_too_large"] = len(dropped)
        result.files = kept

    return result


_BIB_EXT = {".bib"}
_CSL_EXT = {".csl"}
_STYLE_EXT = {".sty"}
_CLASS_EXT = {".cls"}
_CONFIG_EXT = {".yml", ".yaml", ".toml", ".json"}

_METADATA_NAME_HINTS = re.compile(r"(front[\-_ ]?matter|meta|metadata|titlepage|abstract|cover)", re.I)
_STYLE_NAME_HINTS = re.compile(r"(style|template|theme|preamble|format)", re.I)
_CONTENT_NAME_HINTS = re.compile(r"(chapter|section|intro|conclusion|appendix|results|methods|discussion|body)", re.I)
_ASSET_META_NAME_HINTS = re.compile(r"(sources|assets|figures|images)", re.I)


def classify_file_role(filename: str, *, is_entrypoint: bool) -> tuple[str, str]:
    if is_entrypoint:
        return FileRole.ENTRYPOINT, "high"

    path = Path(filename)
    suffix = path.suffix.lower()
    stem = path.stem

    if suffix in _BIB_EXT:
        return FileRole.BIB, "high"
    if suffix in _CSL_EXT:
        return FileRole.CSL, "high"
    if suffix in _CLASS_EXT:
        return FileRole.CLASS, "high"
    if suffix in _STYLE_EXT:
        return FileRole.STYLE, "high"
    if suffix in _CONFIG_EXT:
        if _ASSET_META_NAME_HINTS.search(stem):
            return FileRole.ASSET_METADATA, "medium"
        return FileRole.CONFIG, "high"
    if suffix == ".md":
        return FileRole.AUXILIARY, "medium"

    if suffix in {".tex", ".typ"}:
        if _METADATA_NAME_HINTS.search(stem):
            return FileRole.METADATA, "medium"
        if _STYLE_NAME_HINTS.search(stem):
            return FileRole.STYLE, "low"
        if _CONTENT_NAME_HINTS.search(stem):
            return FileRole.CONTENT_SECTION, "medium"
        return FileRole.CONTENT_SECTION, "low"

    return FileRole.UNKNOWN, "low"