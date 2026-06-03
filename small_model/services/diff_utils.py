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
    deleted_headings: list[str]
    deleted_text_samples: list[str]
    added_text_samples: list[str]
    deleted_labels_or_refs: list[str]
    changed_imports_or_includes: list[str]
    content_loss_signals: list[dict[str, Any]]
    semantic_summary: dict[str, Any]


def build_diff_review_input(diff_text: str, *, soft_limit: int = 8192, hard_cap: int = 12288) -> DiffReviewInput:
    files: list[str] = []
    touched_headings: list[str] = []
    deleted_headings: list[str] = []
    deleted_text_samples: list[str] = []
    added_text_samples: list[str] = []
    deleted_labels_or_refs: list[str] = []
    changed_imports_or_includes: list[str] = []
    file_stats: dict[str, dict[str, int]] = {}
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
                file_stats.setdefault(current_file, {"added": 0, "removed": 0, "hunks": 0})
        elif line.startswith("@@"):
            hunks += 1
            if current_file:
                file_stats.setdefault(current_file, {"added": 0, "removed": 0, "hunks": 0})["hunks"] += 1
        elif line.startswith("+") and not line.startswith("+++"):
            lines_added += 1
            if current_file:
                file_stats.setdefault(current_file, {"added": 0, "removed": 0, "hunks": 0})["added"] += 1
            added_text = line[1:].strip()
            if _looks_like_content_line(added_text) and len(added_text_samples) < 20:
                added_text_samples.append(_truncate_sample(added_text))
            changed_imports_or_includes.extend(match.group(0) for match in IMPORT_RE.finditer(line))
        elif line.startswith("-") and not line.startswith("---"):
            lines_removed += 1
            if current_file:
                file_stats.setdefault(current_file, {"added": 0, "removed": 0, "hunks": 0})["removed"] += 1
            removed_text = line[1:].strip()
            if _looks_like_content_line(removed_text) and len(deleted_text_samples) < 20:
                deleted_text_samples.append(_truncate_sample(removed_text))
            deleted_labels_or_refs.extend(match.group(0) for match in LABEL_REF_RE.finditer(line))
            changed_imports_or_includes.extend(match.group(0) for match in IMPORT_RE.finditer(line))

        content = line[1:] if line.startswith(("+", "-", " ")) else line
        heading = HEADING_RE.match(content)
        if heading:
            title = (heading.group(1) or heading.group(2) or "").strip()
            if title and title not in touched_headings:
                touched_headings.append(title)
            if line.startswith("-") and not line.startswith("---") and title and title not in deleted_headings:
                deleted_headings.append(title)

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

    stats = {
        "files_changed": len(files),
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "total_changed_lines": total_changed,
        "hunks": hunks,
        "diff_char_length": len(diff_text or ""),
        "diff_truncated": truncated,
        "truncated_at_chars": soft_limit if truncated else None,
        "file_stats": file_stats,
    }
    content_loss_signals = _build_content_loss_signals(
        stats=stats,
        deleted_headings=deleted_headings,
        deleted_text_samples=deleted_text_samples,
        added_text_samples=added_text_samples,
        deleted_labels_or_refs=deleted_labels_or_refs,
        changed_imports_or_includes=changed_imports_or_includes,
    )

    return DiffReviewInput(
        diff_stats=stats,
        changed_files=files,
        unified_diff=body,
        touched_headings=touched_headings[:20],
        deleted_headings=deleted_headings[:20],
        deleted_text_samples=deleted_text_samples[:20],
        added_text_samples=added_text_samples[:20],
        deleted_labels_or_refs=deleted_labels_or_refs[:50],
        changed_imports_or_includes=changed_imports_or_includes[:50],
        content_loss_signals=content_loss_signals,
        semantic_summary=_build_semantic_summary(
            files=files,
            stats=stats,
            touched_headings=touched_headings,
            deleted_headings=deleted_headings,
            deleted_text_samples=deleted_text_samples,
            content_loss_signals=content_loss_signals,
            deleted_labels_or_refs=deleted_labels_or_refs,
            changed_imports_or_includes=changed_imports_or_includes,
        ),
    )


def _looks_like_content_line(text: str) -> bool:
    value = str(text or "").strip()
    if len(value) < 24:
        return False
    if value.startswith(("\\", "#", "//", "%")):
        return False
    if re.fullmatch(r"[\W_]+", value):
        return False
    return bool(re.search(r"[A-Za-zА-Яа-яІіЇїЄєҐґ]", value))


def _truncate_sample(text: str, max_len: int = 180) -> str:
    value = re.sub(r"\s+", " ", str(text or "").strip())
    return value if len(value) <= max_len else f"{value[: max_len - 1]}…"


def _sample_token_overlap(removed: list[str], added: list[str]) -> float:
    removed_tokens = _content_tokens(" ".join(removed))
    added_tokens = _content_tokens(" ".join(added))
    if not removed_tokens:
        return 1.0
    return len(removed_tokens & added_tokens) / max(1, len(removed_tokens))


def _content_tokens(text: str) -> set[str]:
    tokens = re.findall(r"[A-Za-zА-Яа-яІіЇїЄєҐґ0-9_@.-]{4,}", str(text or "").lower())
    stop = {
        "this", "that", "with", "from", "для", "що", "які", "або", "але", "цей", "цим",
        "такий", "також", "через", "після", "може", "має", "було", "буде",
    }
    return {token for token in tokens if token not in stop}


def _build_content_loss_signals(
    *,
    stats: dict[str, Any],
    deleted_headings: list[str],
    deleted_text_samples: list[str],
    added_text_samples: list[str],
    deleted_labels_or_refs: list[str],
    changed_imports_or_includes: list[str],
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    removed = int(stats.get("lines_removed") or 0)
    added = int(stats.get("lines_added") or 0)
    overlap = _sample_token_overlap(deleted_text_samples, added_text_samples)
    if deleted_headings:
        signals.append({
            "kind": "deleted_heading",
            "severity": "high",
            "title": "Зник заголовок документа",
            "detail": f"Видалено: {', '.join(deleted_headings[:3])}.",
        })
    if deleted_text_samples and removed >= added + 3 and overlap < 0.38:
        signals.append({
            "kind": "unreplaced_prose",
            "severity": "medium",
            "title": "Можлива втрата текстового змісту",
            "detail": "Видалено помітно більше прозового тексту, ніж додано, і новий текст слабо перекривається зі старим.",
            "removed_samples": deleted_text_samples[:3],
            "token_overlap": round(overlap, 3),
        })
    if len(deleted_text_samples) >= 5 and added <= 2:
        signals.append({
            "kind": "bulk_content_drop",
            "severity": "high",
            "title": "Схоже на масове знесення контенту",
            "detail": "Кілька змістовних рядків видалено майже без заміни.",
            "removed_samples": deleted_text_samples[:3],
        })
    if deleted_labels_or_refs:
        signals.append({
            "kind": "deleted_refs",
            "severity": "medium",
            "title": "Зачеплено посилання або цитування",
            "detail": "Видалено labels/references/citations, це може зламати структуру документа.",
        })
    if changed_imports_or_includes:
        signals.append({
            "kind": "changed_imports",
            "severity": "medium",
            "title": "Змінено imports/includes",
            "detail": "Зміна підключень може мати вплив за межами видимого diff.",
        })
    return signals[:8]


def _build_semantic_summary(
    *,
    files: list[str],
    stats: dict[str, Any],
    touched_headings: list[str],
    deleted_headings: list[str],
    deleted_text_samples: list[str],
    content_loss_signals: list[dict[str, Any]],
    deleted_labels_or_refs: list[str],
    changed_imports_or_includes: list[str],
) -> dict[str, Any]:
    added = int(stats.get("lines_added") or 0)
    removed = int(stats.get("lines_removed") or 0)
    files_count = len(files)
    if files_count == 1:
        title = f"Змінено 1 файл: +{added}/-{removed}"
    else:
        title = f"Змінено {files_count} файли(ів): +{added}/-{removed}"

    items: list[dict[str, str]] = []
    file_stats = stats.get("file_stats") if isinstance(stats.get("file_stats"), dict) else {}
    for filename in files[:4]:
        per_file = file_stats.get(filename, {}) if isinstance(file_stats, dict) else {}
        items.append({
            "kind": "file",
            "label": filename,
            "detail": f"+{int(per_file.get('added') or 0)}/-{int(per_file.get('removed') or 0)}",
        })
    if touched_headings:
        items.append({
            "kind": "headings",
            "label": "Зачеплено розділи",
            "detail": ", ".join(touched_headings[:4]),
        })
    if deleted_headings:
        items.append({
            "kind": "deleted_headings",
            "label": "Видалено заголовки",
            "detail": ", ".join(deleted_headings[:4]),
        })
    if deleted_text_samples:
        items.append({
            "kind": "content",
            "label": "Переписано/видалено змістовний текст",
            "detail": f"{len(deleted_text_samples)} фрагмент(ів) старого тексту у diff.",
        })
    if deleted_labels_or_refs:
        items.append({
            "kind": "refs",
            "label": "Зачеплено посилання",
            "detail": f"{len(deleted_labels_or_refs)} label/ref/cite.",
        })
    if changed_imports_or_includes:
        items.append({
            "kind": "imports",
            "label": "Зачеплено підключення",
            "detail": f"{len(changed_imports_or_includes)} import/include/bibliography.",
        })

    impact = "low"
    if deleted_headings or any(signal.get("severity") == "high" for signal in content_loss_signals):
        impact = "high"
    elif removed > added + 3 or files_count > 1 or content_loss_signals:
        impact = "medium"

    return {
        "title": title,
        "impact": impact,
        "items": items[:8],
        "content_loss_signals": content_loss_signals,
        "stats": {
            "files_changed": files_count,
            "lines_added": added,
            "lines_removed": removed,
            "hunks": int(stats.get("hunks") or 0),
        },
    }


def warning(severity: str, code: str, message: str, source: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"severity": severity, "code": code, "message": message, "source": source}
    payload.update({key: value for key, value in extra.items() if value is not None})
    return payload
