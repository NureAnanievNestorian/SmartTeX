"""Deterministic structural extraction: headings, metadata blocks, edit triggers."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from SmartTeX.markup import MarkupType
from projects.services import (
    SectionChunk,
    split_tex_sections,
    split_typst_sections,
)

from ..models import RegionKind


# Matches Typst `#let identifier = ...` at top level.
TYPST_LET_RE = re.compile(r"^\s*#let\s+([A-Za-z_][A-Za-z0-9_-]*)\b", re.MULTILINE)



@dataclass
class RegionInfo:
    region_kind: str
    title: str
    level: Optional[int]
    order: int
    line_start: int
    line_end: int
    content_hash: str


def _region_hash(text: str, start: int, end: int) -> str:
    body = "\n".join(text.splitlines()[max(0, start - 1): max(0, end)])
    return hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()


def extract_regions(
    *, filename: str, content: str, markup_type: str, role: str
) -> list[RegionInfo]:
    """Extract region cards for a file. Only meaningful for source/metadata files."""
    suffix = Path(filename).suffix.lower()
    regions: list[RegionInfo] = []

    if suffix == ".typ":
        chunks = split_typst_sections(content)
        regions.extend(_chunks_to_regions(chunks, content))
        # Also surface top-level `#let X =` metadata blocks.
        for order_offset, m in enumerate(TYPST_LET_RE.finditer(content), start=len(regions)):
            line_start = content.count("\n", 0, m.start()) + 1
            line_end = line_start
            regions.append(
                RegionInfo(
                    region_kind=RegionKind.METADATA_BLOCK,
                    title=f"#let {m.group(1)}",
                    level=None,
                    order=order_offset,
                    line_start=line_start,
                    line_end=line_end,
                    content_hash=_region_hash(content, line_start, line_end),
                )
            )
    elif suffix == ".tex":
        chunks = split_tex_sections(content)
        regions.extend(_chunks_to_regions(chunks, content))
    elif suffix == ".bib":
        # Bibliography files get a single region representing the whole file.
        total_lines = max(1, content.count("\n") + 1)
        regions.append(
            RegionInfo(
                region_kind=RegionKind.BIBLIOGRAPHY_BLOCK,
                title=Path(filename).name,
                level=None,
                order=0,
                line_start=1,
                line_end=total_lines,
                content_hash=_region_hash(content, 1, total_lines),
            )
        )

    # Re-normalize order to be contiguous.
    regions.sort(key=lambda r: (r.line_start, r.order))
    for i, r in enumerate(regions):
        r.order = i
    return regions


def _chunks_to_regions(chunks: list[SectionChunk], content: str) -> list[RegionInfo]:
    out: list[RegionInfo] = []
    for chunk in chunks:
        kind = RegionKind.HEADING_SECTION if chunk.level > 0 else RegionKind.FRONT_MATTER
        out.append(
            RegionInfo(
                region_kind=kind,
                title=chunk.title or "",
                level=chunk.level or None,
                order=chunk.index,
                line_start=max(1, chunk.start_line),
                line_end=max(chunk.start_line, chunk.end_line),
                content_hash=_region_hash(content, chunk.start_line, chunk.end_line),
            )
        )
    return out


# --- Trigger generation ------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")


def deterministic_file_triggers(
    *, filename: str, role: str, region_titles: list[str]
) -> list[dict]:
    """Generate deterministic edit triggers for a file."""
    triggers: list[dict] = []
    seen: set[str] = set()

    def add(phrase: str, kind: str, confidence: str) -> None:
        norm = phrase.strip().lower()
        if not norm or norm in seen:
            return
        seen.add(norm)
        triggers.append({"phrase": phrase, "kind": kind, "confidence": confidence})

    path = Path(filename)
    stem = path.stem
    add(stem, kind="literal", confidence="medium")
    add(filename, kind="literal", confidence="high")

    # Role keywords
    role_keywords = {
        "bib": ["bibliography", "references", "citations"],
        "csl": ["citation style"],
        "style": ["style", "preamble"],
        "class": ["document class"],
        "metadata": ["front matter", "metadata", "title page"],
        "asset_metadata": ["assets", "figures"],
        "config": ["config", "settings"],
        "entrypoint": ["main document"],
    }
    for kw in role_keywords.get(role, []):
        add(kw, kind="semantic", confidence="medium")

    for title in region_titles:
        if title:
            add(title, kind="literal", confidence="medium")
            for w in _WORD_RE.findall(title):
                if len(w) >= 4:
                    add(w, kind="semantic", confidence="low")

    return triggers[:25]


def deterministic_region_triggers(*, title: str) -> list[dict]:
    triggers: list[dict] = []
    if title:
        triggers.append({"phrase": title, "kind": "literal", "confidence": "medium"})
        for w in _WORD_RE.findall(title):
            if len(w) >= 4:
                triggers.append({"phrase": w, "kind": "semantic", "confidence": "low"})
    return triggers[:10]
