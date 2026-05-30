from __future__ import annotations

from typing import Any

from .provider import SmallModelProvider, SmallModelResponse, estimate_tokens
from .task_types import (
    TASK_CIRCUIT_BREAKER_EVALUATE,
    TASK_COMPILE_LOG_TRIAGE,
    TASK_CONTEXT_COMPRESS,
    TASK_DIFF_SAFETY_REVIEW,
    TASK_EDIT_INTENT_CLASSIFY,
    TASK_NAV_FILE_CARD_ENRICH,
    TASK_NAV_REGION_CARD_ENRICH,
    TASK_NAV_RERANK_TARGETS,
    TASK_NAV_REPAIR_GUIDANCE,
    TASK_PRE_PROPOSAL_ANALYZE,
)


DEFAULT_RESPONSES: dict[str, dict[str, Any]] = {
    TASK_PRE_PROPOSAL_ANALYZE: {
        "task_brief": "",
        "relevant_files": [],
        "relevant_section_ids": [],
        "relevant_summaries": [],
        "do_not_touch_files": [],
        "do_not_touch_section_ids": [],
        "recommended_read_strategy": "range_only",
        "max_read_lines": 100,
        "edit_mode": "paragraph_edit",
        "allowed_ops": ["patch_file_lines", "replace_in_project_file", "propose_document_change"],
        "forbidden_ops": ["update_project_section", "update_project_file"],
        "max_files": 1,
        "max_changed_lines": 15,
        "read_strategy": "range_only",
        "compile_required": True,
        "requires_user_clarification": False,
        "clarification_reason": None,
    },
    TASK_CONTEXT_COMPRESS: {
        "task_brief": "",
        "relevant_files": [],
        "relevant_section_ids": [],
        "relevant_summaries": [],
        "do_not_touch_files": [],
        "do_not_touch_section_ids": [],
        "recommended_read_strategy": "range_only",
        "max_read_lines": 100,
    },
    TASK_EDIT_INTENT_CLASSIFY: {
        "edit_mode": "paragraph_edit",
        "allowed_ops": ["patch_file_lines", "replace_in_project_file", "propose_document_change"],
        "forbidden_ops": ["update_project_section", "update_project_file"],
        "max_files": 1,
        "max_changed_lines": 15,
        "read_strategy": "range_only",
        "compile_required": True,
        "requires_user_clarification": False,
        "clarification_reason": None,
    },
    TASK_DIFF_SAFETY_REVIEW: {
        "risk_level": "low",
        "overedit_detected": False,
        "unrelated_changes_detected": False,
        "suspicious_deletions": [],
        "deleted_labels_or_refs": [],
        "changed_imports_or_includes": [],
        "recommendation": "allow",
        "rejection_reason": None,
    },
    TASK_COMPILE_LOG_TRIAGE: {
        "error_category": "unknown",
        "error_origin": "patch_error",
        "likely_file": None,
        "likely_line": None,
        "likely_cause": "",
        "safe_fix_strategy": "",
        "safe_to_retry": False,
        "retry_scope": "do_not_retry",
    },
    TASK_CIRCUIT_BREAKER_EVALUATE: {
        "decision": "continue",
        "reason": "",
        "suggested_scope_reduction": None,
    },
    TASK_NAV_FILE_CARD_ENRICH: {
        "summary": "mock file summary",
        "state": "real",
        "state_confidence": "medium",
        "summary_confidence": "medium",
        "role_refinement": None,
        "role_confidence": None,
        "edit_triggers": [{"phrase": "mock-trigger", "weight": 1.0}],
    },
    TASK_NAV_REGION_CARD_ENRICH: {
        "summary": "mock region summary",
        "state": "real",
        "state_confidence": "medium",
        "summary_confidence": "medium",
        "edit_triggers": [{"phrase": "mock-region", "weight": 1.0}],
    },
    TASK_NAV_RERANK_TARGETS: {
        "ranked": [],
        "scope_confidence": "medium",
    },
    TASK_NAV_REPAIR_GUIDANCE: {
        "error_kind": "other",
        "diagnosis": "mock diagnosis",
        "fix_hint": {
            "rewrite_op": None,
            "add_op": None,
            "additional_read_targets": [],
            "notes": None,
        },
    },
}


class MockProvider(SmallModelProvider):
    provider_name = "mock"

    def __init__(self, responses: dict[str, dict[str, Any]] | None = None):
        self.responses = {**DEFAULT_RESPONSES, **(responses or {})}

    def generate_json(
        self,
        *,
        task_type: str,
        system_instruction: str,
        input_payload: dict[str, Any],
        response_schema: dict[str, Any],
        user,
        project,
        timeout_seconds: int,
    ) -> SmallModelResponse:
        parsed = dict(self.responses.get(task_type, {}))
        return SmallModelResponse(
            success=True,
            parsed_json=parsed,
            raw_text="",
            provider_name=self.provider_name,
            model_name="mock",
            input_tokens_estimate=estimate_tokens(input_payload),
            output_tokens_estimate=estimate_tokens(parsed, source_like=False),
        )
