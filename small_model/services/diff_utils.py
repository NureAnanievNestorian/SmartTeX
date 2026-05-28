from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


LABEL_REF_RE = re.compile(r"(\\(?:label|ref|cite)\{|#(?:label|ref|cite)\(|<[^>\n]+>)")
IMPORT_RE = re.compile(r"(\\(?:usepackage|input|include)\{|#(?:import|include|bibliography)\b)")
HEADING_RE = re.compile(r"^\s*(?:\\(?:chapter|section|subsection|subsubsection)\{([^}]+)\}|={1,6}\s+(.+?)\s*$)")


@dataclass(slots=True)
class DiffReviewInput:
    diff_stats: dict[str, Any]
    changed_files: list[str]
    unified_diff: str
    touched_headings: list[str]
    deleted_labels_or_refs: list[str]
    changed_imports_or_includes: list[str]


def build_diff_review_input(diff_text: str, *, soft_limit: int = 8192, hard_cap: int = 12288) -> DiffReviewInput:
    files: list[str] = []
    touched_headings: list[str] = []
    deleted_labels_or_refs: list[str] = []
    changed_imports_or_includes: list[str] = []
    lines_added = 0
    lines_removed = 0
    hunks = 0
    selected: list[str] = []
    selected_len = 0
    current_file = ""

    for line in str(diff_text or "").splitlines(keepends=True):
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            if current_file and current_file not in files:
                files.append(current_file)
        elif line.startswith("@@"):
            hunks += 1
        elif line.startswith("+") and not line.startswith("+++"):
            lines_added += 1
            changed_imports_or_includes.extend(match.group(0) for match in IMPORT_RE.finditer(line))
        elif line.startswith("-") and not line.startswith("---"):
            lines_removed += 1
            deleted_labels_or_refs.extend(match.group(0) for match in LABEL_REF_RE.finditer(line))
            changed_imports_or_includes.extend(match.group(0) for match in IMPORT_RE.finditer(line))

        content = line[1:] if line.startswith(("+", "-", " ")) else line
        heading = HEADING_RE.match(content)
        if heading:
            title = (heading.group(1) or heading.group(2) or "").strip()
            if title and title not in touched_headings:
                touched_headings.append(title)

        relevant = (
            line.startswith(("diff --git", "--- ", "+++ ", "@@", "+", "-"))
            or bool(heading)
            or bool(LABEL_REF_RE.search(line))
            or bool(IMPORT_RE.search(line))
        )
        if relevant and selected_len < soft_limit:
            remaining = soft_limit - selected_len
            chunk = line[:remaining]
            selected.append(chunk)
            selected_len += len(chunk)

    total_changed = lines_added + lines_removed
    truncated = len(diff_text or "") > soft_limit
    body = "".join(selected)
    if truncated:
        body += "\n...TRUNCATED..."
    if len(body) > hard_cap:
        body = body[:hard_cap] + "\n...TRUNCATED..."
        truncated = True

    return DiffReviewInput(
        diff_stats={
            "files_changed": len(files),
            "lines_added": lines_added,
            "lines_removed": lines_removed,
            "total_changed_lines": total_changed,
            "hunks": hunks,
            "diff_char_length": len(diff_text or ""),
            "diff_truncated": truncated,
            "truncated_at_chars": soft_limit if truncated else None,
        },
        changed_files=files,
        unified_diff=body,
        touched_headings=touched_headings[:20],
        deleted_labels_or_refs=deleted_labels_or_refs[:50],
        changed_imports_or_includes=changed_imports_or_includes[:50],
    )


def warning(severity: str, code: str, message: str, source: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message, "source": source}
