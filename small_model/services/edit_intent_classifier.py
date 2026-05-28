from __future__ import annotations

from small_model import schemas
from small_model.services.base import SmallModelCallMixin
from small_model.services.payload import PayloadSanitizer
from small_model.task_types import FEATURE_EDIT_INTENT_CLASSIFIER, TASK_EDIT_INTENT_CLASSIFY


EDIT_MODE_BUDGETS = {
    "micro_edit": (5, 1, ["update_project_section", "update_project_file"]),
    "paragraph_edit": (15, 1, ["update_project_section", "update_project_file"]),
    "section_edit": (50, 2, ["update_project_file"]),
    "new_section": (80, 2, ["update_project_file"]),
    "compile_fix": (20, 3, ["update_project_file"]),
    "review_only": (0, 0, ["patch_file_lines", "replace_in_project_file", "propose_document_change", "update_project_section", "update_project_file"]),
}

CONSERVATIVE_PARAGRAPH = {
    "edit_mode": "paragraph_edit",
    "allowed_ops": ["patch_file_lines", "replace_in_project_file", "propose_document_change"],
    "forbidden_ops": ["update_project_section", "update_project_file"],
    "max_files": 1,
    "max_changed_lines": 15,
    "read_strategy": "range_only",
    "compile_required": True,
    "requires_user_clarification": False,
    "clarification_reason": None,
}


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
        result = {**CONSERVATIVE_PARAGRAPH, **response.parsed_json}
        mode = result.get("edit_mode")
        if mode in EDIT_MODE_BUDGETS:
            max_lines, max_files, forbidden = EDIT_MODE_BUDGETS[mode]
            result["max_changed_lines"] = min(int(result.get("max_changed_lines") or max_lines), max_lines)
            result["max_files"] = min(int(result.get("max_files") or max_files), max_files)
            result["forbidden_ops"] = sorted(set(result.get("forbidden_ops") or []) | set(forbidden))
        return result
