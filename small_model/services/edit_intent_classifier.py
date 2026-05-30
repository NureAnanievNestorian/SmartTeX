from __future__ import annotations

from small_model import schemas
from small_model.services.base import SmallModelCallMixin
from small_model.services.payload import PayloadSanitizer
from small_model.task_types import FEATURE_EDIT_INTENT_CLASSIFIER, TASK_EDIT_INTENT_CLASSIFY


EDIT_MODE_BUDGETS = {
    "micro_edit": (12, 1, ["update_project_section", "update_project_file"]),
    "paragraph_edit": (25, 1, ["update_project_section", "update_project_file"]),
    "section_edit": (50, 2, ["update_project_file"]),
    "new_section": (80, 2, ["update_project_file"]),
    "compile_fix": (20, 3, ["update_project_file"]),
    "review_only": (0, 0, ["patch_file_lines", "replace_in_project_file", "propose_document_change", "update_project_section", "update_project_file"]),
}

# Max number of diff hunks allowed per edit mode before flagging over-scatter.
EDIT_MODE_MAX_HUNKS: dict[str, int] = {
    "micro_edit": 2,
    "paragraph_edit": 5,
    "section_edit": 15,
    "new_section": 15,
    "compile_fix": 10,
    "review_only": 0,
}

_DEFAULT_ALLOWED_OPS = [
    "find_project_files", "grep_file", "read_file_lines",
    "replace_exact", "patch_file_lines", "replace_in_project_file", "propose_document_change",
]
_DEFAULT_FORBIDDEN_OPS = [
    "read_project_file", "update_project_file", "update_project_section",
    "create_new_file", "delete_file", "rename_file",
    "rewrite_section", "full_file_overwrite", "arbitrary_shell",
]

CONSERVATIVE_PARAGRAPH = {
    "edit_mode": "paragraph_edit",
    "allowed_ops": _DEFAULT_ALLOWED_OPS,
    "forbidden_ops": _DEFAULT_FORBIDDEN_OPS,
    "max_files": 1,
    "max_changed_lines": 15,
    "max_read_lines": 120,
    "read_strategy": "range_only",
    "recommended_read_strategy": "range_only",
    "compile_required": False,
    "requires_user_clarification": False,
    "clarification_reason": None,
    "scope_confidence": "high",
    "scope_confidence_reason": None,
}

COMPILE_FIX_FALLBACK = {
    "edit_mode": "compile_fix",
    "allowed_ops": _DEFAULT_ALLOWED_OPS,
    "forbidden_ops": _DEFAULT_FORBIDDEN_OPS,
    "max_files": 2,
    "max_changed_lines": 20,
    "max_read_lines": 120,
    "read_strategy": "range_only",
    "recommended_read_strategy": "range_only",
    "compile_required": True,
    "requires_user_clarification": False,
    "clarification_reason": None,
    "scope_confidence": "high",
    "scope_confidence_reason": None,
}


def sanitize_smcl_edit_intent(data: dict) -> tuple[dict, bool]:
    """Validate SMCL edit-intent output against strict enums; return (sanitized, fallback_used)."""
    fallback_used = False
    result = dict(data)

    mode = result.get("edit_mode")
    if mode not in schemas.VALID_EDIT_MODES:
        result.update(dict(CONSERVATIVE_PARAGRAPH))
        fallback_used = True
        mode = result["edit_mode"]

    for field in ("read_strategy", "recommended_read_strategy"):
        val = result.get(field)
        if val and val not in schemas.VALID_READ_STRATEGIES:
            result[field] = "range_only"
            fallback_used = True

    raw_allowed = result.get("allowed_ops") or []
    filtered_allowed = [op for op in raw_allowed if op in schemas.VALID_ALLOWED_OPS]
    if raw_allowed and not filtered_allowed:
        filtered_allowed = list(_DEFAULT_ALLOWED_OPS)
        fallback_used = True
    elif len(filtered_allowed) < len(raw_allowed):
        fallback_used = True
    result["allowed_ops"] = filtered_allowed

    raw_forbidden = result.get("forbidden_ops") or []
    filtered_forbidden = [op for op in raw_forbidden if op in schemas.VALID_FORBIDDEN_OPS]
    if len(filtered_forbidden) < len(raw_forbidden):
        fallback_used = True
    result["forbidden_ops"] = filtered_forbidden

    if mode in EDIT_MODE_BUDGETS:
        budget_lines, budget_files, budget_forbidden = EDIT_MODE_BUDGETS[mode]
        current_lines = result.get("max_changed_lines") or budget_lines
        result["max_changed_lines"] = min(int(current_lines), budget_lines)
        current_files = result.get("max_files") or budget_files
        result["max_files"] = min(int(current_files), budget_files)
        result["forbidden_ops"] = sorted(set(result["forbidden_ops"]) | set(budget_forbidden))

    raw_read_lines = result.get("max_read_lines")
    if raw_read_lines is not None:
        result["max_read_lines"] = min(int(raw_read_lines), schemas.MAX_READ_LINES_CAP)

    confidence = str(result.get("scope_confidence") or "").lower()
    if confidence not in schemas.VALID_SCOPE_CONFIDENCE:
        # Default to "medium" when the small model didn't or couldn't self-rate.
        # "high" would make the budget a hard cap even on an unknown scope,
        # causing false rejections whenever the model times out or returns garbage.
        confidence = "medium"
        fallback_used = True
    result["scope_confidence"] = confidence
    reason = result.get("scope_confidence_reason")
    result["scope_confidence_reason"] = str(reason)[:300] if isinstance(reason, str) else None

    return result, fallback_used


class EditIntentClassifierService(SmallModelCallMixin):
    feature_key = FEATURE_EDIT_INTENT_CLASSIFIER
    task_type = TASK_EDIT_INTENT_CLASSIFY

    def run(self, *, user, project, user_request: str, selected_file: str | None = None, selected_section_id: str | None = None) -> dict:
        enabled, _, _ = self.is_enabled(user, project)
        if not enabled:
            return {}
        payload = PayloadSanitizer.clean_payload(
            {
                "user_request": PayloadSanitizer.trim_text(user_request, max_chars=2000),
                "selected_file": selected_file,
                "selected_section_id": selected_section_id,
                "document_type": getattr(project, "markup_type", ""),
                "current_task_title": None,
                "outline_item_type": None,
            }
        )
        response = self.call_provider(
            user=user,
            project=project,
            system_instruction="Classify the edit scope. Prefer narrower patch budgets when uncertain.",
            input_payload=payload,
            response_schema=schemas.EDIT_INTENT_SCHEMA,
        )
        if not response.success or not response.parsed_json:
            return dict(CONSERVATIVE_PARAGRAPH)
        merged = {**CONSERVATIVE_PARAGRAPH, **response.parsed_json}
        result, _ = sanitize_smcl_edit_intent(merged)
        return result
