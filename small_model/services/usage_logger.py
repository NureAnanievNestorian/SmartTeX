from __future__ import annotations

import logging

from small_model.models import SmallModelUsageLog
from small_model.provider import SmallModelResponse

logger = logging.getLogger(__name__)


class SmallModelUsageLogger:
    @staticmethod
    def log(
        user,
        project,
        task_type: str,
        response: SmallModelResponse,
        *,
        input_prompt: str = "",
        output_text: str = "",
    ) -> None:
        try:
            status = SmallModelUsageLog.Status.SUCCESS
            if not response.success:
                code = (response.error_code or "PROVIDER_ERROR").upper()
                status = {
                    "TIMEOUT": SmallModelUsageLog.Status.TIMEOUT,
                    "INVALID_JSON": SmallModelUsageLog.Status.INVALID_JSON,
                    "QUOTA_EXCEEDED": SmallModelUsageLog.Status.QUOTA_EXCEEDED,
                }.get(code, SmallModelUsageLog.Status.PROVIDER_ERROR)
            SmallModelUsageLog.objects.create(
                user=user,
                project=project,
                provider=response.provider_name,
                model_name=response.model_name,
                task_type=task_type,
                status=status,
                input_tokens_estimate=response.input_tokens_estimate,
                output_tokens_estimate=response.output_tokens_estimate,
                latency_ms=response.latency_ms,
                error_code=response.error_code or "",
                error_message=response.error_message or "",
                input_prompt=input_prompt,
                output_text=output_text,
            )
        except Exception:
            logger.exception("Failed to write small model usage log")
