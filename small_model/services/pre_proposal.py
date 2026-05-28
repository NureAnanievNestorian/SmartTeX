from __future__ import annotations

from small_model import schemas
from small_model.services.base import SmallModelCallMixin
from small_model.services.edit_intent_classifier import CONSERVATIVE_PARAGRAPH, EDIT_MODE_BUDGETS
from small_model.services.payload import PayloadSanitizer
from small_model.task_types import FEATURE_EDIT_INTENT_CLASSIFIER, TASK_PRE_PROPOSAL_ANALYZE


class PreProposalAnalysisService(SmallModelCallMixin):
    feature_key = FEATURE_EDIT_INTENT_CLASSIFIER
    task_type = TASK_PRE_PROPOSAL_ANALYZE

    def run(self, *, user, project, user_request: str, selected_file: str | None = None, selected_section_id: str | None = None) -> dict:
        enabled, _, _ = self.is_enabled(user, project)
        if not enabled:
            return {}
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
            system_instruction=(
                "Return compact editing context guidance and classify edit scope as JSON. "
                "Prefer narrower patch budgets when uncertain. Do not include raw document text."
            ),
            input_payload=payload,
            response_schema=schemas.PRE_PROPOSAL_SCHEMA,
        )
        if not response.success or not response.parsed_json:
            return {"edit_intent": dict(CONSERVATIVE_PARAGRAPH), "context_compressor": {}}
        parsed = dict(response.parsed_json)
        edit_intent = {**CONSERVATIVE_PARAGRAPH, **parsed}
        mode = edit_intent.get("edit_mode")
        if mode in EDIT_MODE_BUDGETS:
            max_lines, max_files, forbidden = EDIT_MODE_BUDGETS[mode]
            edit_intent["max_changed_lines"] = min(int(edit_intent.get("max_changed_lines") or max_lines), max_lines)
            edit_intent["max_files"] = min(int(edit_intent.get("max_files") or max_files), max_files)
            edit_intent["forbidden_ops"] = sorted(set(edit_intent.get("forbidden_ops") or []) | set(forbidden))
        context = {
            "task_brief": parsed.get("task_brief", ""),
            "relevant_files": parsed.get("relevant_files") or [],
            "relevant_section_ids": parsed.get("relevant_section_ids") or [],
            "relevant_summaries": parsed.get("relevant_summaries") or [],
            "do_not_touch_files": parsed.get("do_not_touch_files") or [],
            "do_not_touch_section_ids": parsed.get("do_not_touch_section_ids") or [],
            "recommended_read_strategy": parsed.get("recommended_read_strategy", ""),
            "max_read_lines": parsed.get("max_read_lines"),
        }
        return {"edit_intent": edit_intent, "context_compressor": context}
