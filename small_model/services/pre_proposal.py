from __future__ import annotations

import re

from small_model import schemas
from small_model.services.base import SmallModelCallMixin
from small_model.services.edit_intent_classifier import (
    COMPILE_FIX_FALLBACK,
    CONSERVATIVE_PARAGRAPH,
    EDIT_MODE_BUDGETS,
    sanitize_smcl_edit_intent,
)
from small_model.services.payload import PayloadSanitizer
from small_model.task_types import FEATURE_EDIT_INTENT_CLASSIFIER, TASK_PRE_PROPOSAL_ANALYZE

_REPLACE_VERBS_RE = re.compile(r"\b(замінити|replace|change|rename|поміняти)\b", re.I)
_BROAD_SCOPE_RE = re.compile(
    r"\b(section|розділ|додати|add|створити|create|переписати|rewrite|refactor|move|перемістити|restructure|новий)\b",
    re.I,
)
_COMPILE_RE = re.compile(
    r"\b(compile|компіляці|bibliography|bib|import|include|\.bib|\.csl|sources\.yml|error|помилк|fix|виправ)\b",
    re.I,
)
_PATH_SAFE_RE = re.compile(r"^[a-zA-Z0-9_\-./]+$")

_SYSTEM_INSTRUCTION = (
    "Analyze the user request and return editing guidance as JSON with STRICT enum values only.\n"
    "edit_mode MUST be exactly one of: micro_edit, paragraph_edit, section_edit, new_section, compile_fix, review_only.\n"
    "read_strategy and recommended_read_strategy MUST be exactly one of: "
    "file_map_only, summary_only, range_only, target_file_if_under_cap. NEVER use 'full'.\n"
    "allowed_ops MUST only contain values from: "
    "find_project_files, file_line_count, grep_file, read_file_lines, replace_exact, "
    "patch_file_lines, insert_after_anchor, insert_before_anchor, replace_between_anchors, "
    "append_to_file, propose_document_change.\n"
    "forbidden_ops MUST only contain values from: "
    "read_project_file, update_project_file, update_project_section, create_new_file, "
    "delete_file, rename_file, rewrite_section, full_file_overwrite, arbitrary_shell.\n"
    "Include candidate_files (list of likely relevant files with path, confidence, reason) "
    "and read_plan (narrow file reads using only: find_project_files, grep_file, read_file_lines).\n"
    "If the request involves bibliography, imports, CSL, compile errors, or missing files, use edit_mode 'compile_fix'.\n"
    "Prefer range_only when uncertain. Prefer narrower patch budgets when uncertain. "
    "Do not include raw document text. Do not suggest reading entire main files."
)


def _is_safe_path(path: str) -> bool:
    if not path or ".." in path or path.startswith("/"):
        return False
    return bool(_PATH_SAFE_RE.match(path))


def _sanitize_candidate_files(raw: list) -> list:
    result = []
    for item in raw[: schemas.MAX_CANDIDATE_FILES]:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        if not _is_safe_path(path):
            continue
        confidence = item.get("confidence", "low")
        if confidence not in ("low", "medium", "high"):
            confidence = "low"
        result.append({
            "path": path,
            "confidence": confidence,
            "reason": str(item.get("reason") or "")[:200],
        })
    return result


def _sanitize_read_plan(raw: list) -> list:
    result = []
    for step in raw[: schemas.MAX_READ_PLAN_STEPS]:
        if not isinstance(step, dict):
            continue
        tool = str(step.get("tool") or "")
        if tool not in schemas.VALID_READ_PLAN_TOOLS:
            continue
        target_file = step.get("target_file")
        if target_file is not None and not _is_safe_path(str(target_file)):
            target_file = None
        result.append({
            "tool": tool,
            "target_file": target_file,
            "pattern": step.get("pattern"),
            "context_lines": step.get("context_lines"),
            "max_lines": step.get("max_lines"),
            "reason": str(step.get("reason") or "")[:200],
        })
    return result


class PreProposalAnalysisService(SmallModelCallMixin):
    feature_key = FEATURE_EDIT_INTENT_CLASSIFIER
    task_type = TASK_PRE_PROPOSAL_ANALYZE

    def run(self, *, user, project, user_request: str, selected_file: str | None = None, selected_section_id: str | None = None) -> dict:
        enabled, _, _ = self.is_enabled(user, project)
        if not enabled:
            return {}
        deterministic = self._deterministic_fast_path(user_request)
        if deterministic is not None:
            return deterministic
        payload = PayloadSanitizer.clean_payload(
            {
                "project_overview": PayloadSanitizer.trim_text(getattr(project, "title", ""), max_chars=500),
                "document_type": getattr(project, "markup_type", ""),
                "outline_items": [],
                "task_metadata": {},
                "document_graph_summary": "",
                "user_request": PayloadSanitizer.trim_text(user_request, max_chars=2000),
                "selected_file": selected_file,
                "selected_section_id": selected_section_id,
                "editing_limits": {"max_changed_lines": 50, "max_files": 5},
            }
        )
        response = self.call_provider(
            user=user,
            project=project,
            system_instruction=_SYSTEM_INSTRUCTION,
            input_payload=payload,
            response_schema=schemas.PRE_PROPOSAL_SCHEMA,
        )
        if not response.success or not response.parsed_json:
            fallback = COMPILE_FIX_FALLBACK if _COMPILE_RE.search(str(user_request or "")) else CONSERVATIVE_PARAGRAPH
            return {"edit_intent": dict(fallback), "context_compressor": {}, "smcl_fallback_used": True}

        parsed = dict(response.parsed_json)
        merged = {**CONSERVATIVE_PARAGRAPH, **parsed}
        edit_intent, fallback_used = sanitize_smcl_edit_intent(merged)

        context = {
            "task_brief": parsed.get("task_brief", ""),
            "relevant_files": parsed.get("relevant_files") or [],
            "relevant_section_ids": parsed.get("relevant_section_ids") or [],
            "relevant_summaries": parsed.get("relevant_summaries") or [],
            "do_not_touch_files": parsed.get("do_not_touch_files") or [],
            "do_not_touch_section_ids": parsed.get("do_not_touch_section_ids") or [],
            "recommended_read_strategy": edit_intent.get("recommended_read_strategy", "range_only"),
            "max_read_lines": edit_intent.get("max_read_lines"),
            "candidate_files": _sanitize_candidate_files(parsed.get("candidate_files") or []),
            "read_plan": _sanitize_read_plan(parsed.get("read_plan") or []),
            "forbidden_reads": [r for r in (parsed.get("forbidden_reads") or []) if isinstance(r, str)][:10],
        }
        return {"edit_intent": edit_intent, "context_compressor": context, "smcl_fallback_used": fallback_used}

    def _deterministic_fast_path(self, user_request: str) -> dict | None:
        text = str(user_request or "").strip()
        if not text:
            return None
        if _COMPILE_RE.search(text) and not _REPLACE_VERBS_RE.search(text):
            edit_intent = {**COMPILE_FIX_FALLBACK}
            context = {
                "task_brief": PayloadSanitizer.trim_text(text, max_chars=200),
                "relevant_files": [],
                "relevant_section_ids": [],
                "relevant_summaries": [],
                "do_not_touch_files": [],
                "do_not_touch_section_ids": [],
                "recommended_read_strategy": "range_only",
                "max_read_lines": 120,
                "candidate_files": [],
                "read_plan": [],
                "forbidden_reads": ["read_project_file", "full_project_read", "full_main_file_read"],
            }
            return {"edit_intent": edit_intent, "context_compressor": context, "smcl_fallback_used": False}
        if not _REPLACE_VERBS_RE.search(text):
            return None
        if _BROAD_SCOPE_RE.search(text):
            return None
        edit_intent = {
            **CONSERVATIVE_PARAGRAPH,
            "edit_mode": "micro_edit",
            "max_changed_lines": 5,
            "max_files": 1,
        }
        context = {
            "task_brief": PayloadSanitizer.trim_text(text, max_chars=200),
            "relevant_files": [],
            "relevant_section_ids": [],
            "relevant_summaries": [],
            "do_not_touch_files": [],
            "do_not_touch_section_ids": [],
            "recommended_read_strategy": "range_only",
            "max_read_lines": 80,
            "candidate_files": [],
            "read_plan": [],
            "forbidden_reads": [],
        }
        return {"edit_intent": edit_intent, "context_compressor": context, "smcl_fallback_used": False}
