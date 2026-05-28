from __future__ import annotations

from pathlib import Path
from typing import Any

from longdoc.session_service import SessionWriteError, _safe_session_rel_path
from projects.services import project_dir


START_MARKERS = ("smcl:do_not_touch:start", "do_not_touch:start")
END_MARKERS = ("smcl:do_not_touch:end", "do_not_touch:end")
FILE_MARKERS = ("smcl:do_not_touch:file", "do_not_touch:file")


def validate_do_not_touch(project, patch_ops: list[dict[str, Any]], *, root: Path | None = None) -> None:
    protected_files = _collect_op_list(patch_ops, "do_not_touch_files")
    protected_sections = _collect_op_list(patch_ops, "do_not_touch_section_ids") | _collect_op_list(patch_ops, "do_not_touch_sections")
    protected_ranges = _collect_ranges(patch_ops)
    base = root or project_dir(project)

    for op in patch_ops:
        filename = _safe_session_rel_path(str(op.get("filename") or "")).as_posix()
        if filename in protected_files:
            _reject("DO_NOT_TOUCH_FILE", f"{filename} is marked do_not_touch.")
        target = base / filename
        existing = target.read_text(encoding="utf-8", errors="ignore") if target.exists() else ""
        if any(marker in existing for marker in FILE_MARKERS):
            _reject("DO_NOT_TOUCH_FILE", f"{filename} contains a file-level do_not_touch marker.")
        if _touches_protected_range(op, existing, protected_ranges.get(filename, [])):
            _reject("DO_NOT_TOUCH_RANGE", f"{filename} changes a do_not_touch range.")
        if protected_sections and _touches_protected_section(op, existing, protected_sections):
            _reject("DO_NOT_TOUCH_SECTION", f"{filename} changes a do_not_touch section.")


def _collect_op_list(patch_ops: list[dict[str, Any]], key: str) -> set[str]:
    values: set[str] = set()
    for op in patch_ops:
        raw = op.get(key) or []
        if isinstance(raw, str):
            values.add(raw)
        elif isinstance(raw, list):
            values.update(str(item) for item in raw if item)
    return values


def _collect_ranges(patch_ops: list[dict[str, Any]]) -> dict[str, list[tuple[int, int]]]:
    ranges: dict[str, list[tuple[int, int]]] = {}
    for op in patch_ops:
        for item in op.get("do_not_touch_ranges") or []:
            if not isinstance(item, dict):
                continue
            filename = _safe_session_rel_path(str(item.get("filename") or op.get("filename") or "")).as_posix()
            start = int(item.get("start_line") or 0)
            end = int(item.get("end_line") or 0)
            if filename and start > 0 and end >= start:
                ranges.setdefault(filename, []).append((start, end))
    return ranges


def _touches_protected_range(op: dict[str, Any], existing: str, explicit_ranges: list[tuple[int, int]]) -> bool:
    ranges = list(explicit_ranges)
    marker_start: int | None = None
    for idx, line in enumerate(existing.splitlines(), start=1):
        if any(marker in line for marker in START_MARKERS):
            marker_start = idx
        if marker_start is not None and any(marker in line for marker in END_MARKERS):
            ranges.append((marker_start, idx))
            marker_start = None
    if marker_start is not None:
        ranges.append((marker_start, len(existing.splitlines()) or marker_start))
    if not ranges:
        return False
    name = op.get("op")
    if name == "patch_file_lines":
        start = int(op.get("start_line") or 0)
        end = int(op.get("end_line") or start)
        return any(start <= protected_end and end >= protected_start for protected_start, protected_end in ranges)
    if name == "replace_text":
        old_text = str(op.get("old_text") or "")
        for protected_start, protected_end in ranges:
            protected = "\n".join(existing.splitlines()[protected_start - 1 : protected_end])
            if old_text and old_text in protected:
                return True
    if name in {"update_section", "append_to_file"}:
        return True
    return False


def _touches_protected_section(op: dict[str, Any], existing: str, protected_sections: set[str]) -> bool:
    section = str(op.get("section_id") or op.get("section_title") or "")
    if section and section in protected_sections:
        return True
    changed_text = "\n".join(str(op.get(key) or "") for key in ("old_text", "new_content", "content"))
    return any(section and section in changed_text for section in protected_sections) or any(
        section and section in existing and op.get("op") in {"update_section", "replace_text"} for section in protected_sections
    )


def _reject(code: str, message: str) -> None:
    raise SessionWriteError(code, message, status_code=409, suggestion="Submit a narrower patch that excludes do_not_touch content.")
