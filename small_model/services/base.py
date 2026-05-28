from __future__ import annotations

import json
from typing import Any

from django.conf import settings

from small_model.models import ProjectSmallModelSettings, UserSmallModelAccess
from small_model.provider import SmallModelResponse, estimate_tokens
from small_model.registry import get_provider

from .quota_service import SmallModelQuotaService
from .usage_logger import SmallModelUsageLogger

_MAX_LOG_CHARS = 32_000  # cap stored prompt/output to avoid very large rows


class SmallModelCallMixin:
    feature_key: str = ""
    task_type: str = ""

    def is_enabled(self, user, project) -> tuple[bool, UserSmallModelAccess | None, ProjectSmallModelSettings | None]:
        if not bool(getattr(settings, "SMALL_MODEL_FEATURE_ENABLED", False)):
            return False, None, None
        access = UserSmallModelAccess.objects.filter(user=user, enabled=True).first()
        project_settings = ProjectSmallModelSettings.objects.filter(project=project).first()
        if access is None or project_settings is None:
            return False, access, project_settings
        if not access.has_feature(self.feature_key) or not project_settings.feature_enabled(self.feature_key):
            return False, access, project_settings
        return True, access, project_settings

    def call_provider(
        self,
        *,
        user,
        project,
        system_instruction: str,
        input_payload: dict[str, Any],
        response_schema: dict[str, Any],
    ) -> SmallModelResponse:
        quota = SmallModelQuotaService.check_quota(user)
        if not quota.quota_ok:
            return SmallModelResponse(
                success=False,
                provider_name="quota",
                model_name="",
                error_code="QUOTA_EXCEEDED",
                error_message=quota.reason,
            )
        if not SmallModelQuotaService.reserve_request(user):
            return SmallModelResponse(
                success=False,
                provider_name="quota",
                model_name="",
                error_code="QUOTA_EXCEEDED",
                error_message="quota reservation failed",
            )
        log_prompts = bool(getattr(settings, "SMALL_MODEL_LOG_PROMPTS", False))
        response: SmallModelResponse | None = None
        try:
            try:
                access = UserSmallModelAccess.objects.filter(user=user).first()
                provider_name = access.provider if access else None
                provider = get_provider(provider_name)
                timeout = int(getattr(settings, "GEMINI_TIMEOUT_SECONDS", 15))
                response = provider.generate_json(
                    task_type=self.task_type,
                    system_instruction=system_instruction,
                    input_payload=input_payload,
                    response_schema=response_schema,
                    user=user,
                    project=project,
                    timeout_seconds=timeout,
                )
            except Exception as exc:
                response = SmallModelResponse(
                    success=False,
                    provider_name="small_model",
                    model_name="",
                    input_tokens_estimate=estimate_tokens(input_payload),
                    output_tokens_estimate=0,
                    error_code="PROVIDER_ERROR",
                    error_message=str(exc)[:500],
                )
            return response
        finally:
            if response is not None:
                SmallModelQuotaService.consume_tokens(
                    user,
                    response.input_tokens_estimate,
                    response.output_tokens_estimate,
                )
                if log_prompts:
                    try:
                        raw_input = f"[system]\n{system_instruction}\n\n[user]\n{json.dumps(input_payload, ensure_ascii=False)}"
                    except Exception:
                        raw_input = system_instruction
                    logged_input = raw_input[:_MAX_LOG_CHARS]
                    logged_output = (response.raw_text or "")[:_MAX_LOG_CHARS]
                else:
                    logged_input = ""
                    logged_output = ""
                SmallModelUsageLogger.log(
                    user, project, self.task_type, response,
                    input_prompt=logged_input,
                    output_text=logged_output,
                )
