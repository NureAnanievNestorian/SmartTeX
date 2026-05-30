"""Deterministic state classification: real / demo / placeholder / unknown."""
from __future__ import annotations

import re

from ..models import StateKind


PLACEHOLDER_PATTERNS = [
    re.compile(r"\bTODO\b", re.I),
    re.compile(r"\bFIXME\b", re.I),
    re.compile(r"\bTBD\b", re.I),
    re.compile(r"<<\s*placeholder", re.I),
    re.compile(r"\[\s*placeholder", re.I),
    re.compile(r"\bplaceholder text\b", re.I),
    re.compile(r"\bwrite (your|the) (content|text|introduction) here\b", re.I),
    re.compile(r"тут потрібно", re.I),
    re.compile(r"потрібно описати", re.I),
]

DEMO_PATTERNS = [
    re.compile(r"\blorem ipsum\b", re.I),
    re.compile(r"\bdolor sit amet\b", re.I),
    re.compile(r"\bexample\s+(text|content)\b", re.I),
    re.compile(r"\bsample\s+(text|content)\b", re.I),
    re.compile(r"демонстраційн", re.I),
    re.compile(r"тестовий приклад", re.I),
]

DEMO_MACRO_PATTERNS = [
    re.compile(r"\\lipsum"),
    re.compile(r"#lorem\b"),
]


def _strip_comments(text: str) -> str:
    stripped = re.sub(r"%.*", "", text)
    stripped = re.sub(r"//.*", "", stripped)
    return stripped


def classify_state(content: str) -> tuple[str, str]:
    if not content or not content.strip():
        return StateKind.PLACEHOLDER, "high"

    cleaned = _strip_comments(content)

    placeholder_hits = sum(1 for p in PLACEHOLDER_PATTERNS if p.search(cleaned))
    demo_hits = sum(1 for p in DEMO_PATTERNS if p.search(cleaned))
    demo_macro_hits = sum(1 for p in DEMO_MACRO_PATTERNS if p.search(cleaned))

    if demo_hits or demo_macro_hits:
        return StateKind.DEMO, "high" if (demo_hits + demo_macro_hits) > 1 else "medium"

    if placeholder_hits:
        return StateKind.PLACEHOLDER, "high" if placeholder_hits > 1 else "medium"

    visible = re.sub(r"\s+", " ", cleaned).strip()
    if len(visible) < 80:
        return StateKind.PLACEHOLDER, "low"

    return StateKind.UNKNOWN, "low"