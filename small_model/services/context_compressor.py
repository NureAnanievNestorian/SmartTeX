from __future__ import annotations

from small_model import schemas
from small_model.services.base import SmallModelCallMixin
from small_model.services.payload import PayloadSanitizer
from small_model.task_types import FEATURE_CONTEXT_COMPRESSOR, TASK_CONTEXT_COMPRESS


class ContextCompressorService(SmallModelCallMixin):
    feature_key = FEATURE_CONTEXT_COMPRESSOR
    task_type = TASK_CONTEXT_COMPRESS

    def run(self, *, user, project, user_request: str) -> dict:
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
                "editing_limits": {"max_changed_lines": 50, "max_files": 5},
            }
        )
        response = self.call_provider(
            user=user,
            project=project,
            system_instruction="Return compact editing context guidance as JSON. Do not include raw document text.",
            input_payload=payload,
            response_schema=schemas.CONTEXT_COMPRESS_SCHEMA,
        )
        return response.parsed_json if response.success and response.parsed_json else {}
