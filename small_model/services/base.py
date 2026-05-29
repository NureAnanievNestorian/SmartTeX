from __future__ import annotations

import json
import hashlib
from typing import Any

from django.conf import settings
from django.core.cache import cache

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
        access = UserSmallModelAccess.objects.select_related("model_config").filter(user=user).first()
        cfg = access.model_config if access else None
        provider_name = cfg.provider if cfg else None
        model_name = cfg.model_name if cfg else None
        provider_config = dict(cfg.provider_config) if cfg and cfg.provider_config else {}
        provider = get_provider(provider_name, model_name=model_name, config=provider_config)
        effective_provider_name = provider_name or getattr(provider, "provider_name", "")
        provider_model_name = getattr(provider, "model_name", "")
        timeout = int(provider_config.get("timeout_seconds", 15))
        cache_ttl = int(getattr(settings, "SMALL_MODEL_CACHE_TTL_SECONDS", 300))
        cache_key = self._cache_key(
            task_type=self.task_type,
            provider_name=effective_provider_name,
            model_name=str(provider_model_name),
            project_id=getattr(project, "id", 0),
            system_instruction=system_instruction,
            input_payload=input_payload,
        )
        if cache_ttl > 0:
            cached = cache.get(cache_key)
            if cached:
                return SmallModelResponse(**cached)
        quota = SmallModelQuotaService.check_quota(user)
        if not quota.quota_ok:
            return SmallModelResponse(
                success=False,
                provider_name="quota",
                model_name="",
                error_code="QUOTA_EXCEEDED",
                error_message=quota.reason,
            )
        log_prompts = bool(getattr(settings, "SMALL_MODEL_LOG_PROMPTS", False))
        response: SmallModelResponse | None = None
        try:
            try:
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
                if response.success and cache_ttl > 0:
                    cache.set(cache_key, self._serialize_response(response), cache_ttl)
                SmallModelQuotaService.consume_tokens(
                    user,
                    response.input_tokens_estimate,
                    response.output_tokens_estimate,
                    provider=effective_provider_name,
                    model_name=str(provider_model_name),
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

    def _cache_key(
        self,
        *,
        task_type: str,
        provider_name: str,
        model_name: str,
        project_id: int,
        system_instruction: str,
        input_payload: dict[str, Any],
    ) -> str:
        raw = json.dumps(
            {
                "task_type": task_type,
                "provider": provider_name,
                "model": model_name,
                "project_id": project_id,
                "system_instruction": system_instruction,
                "input_payload": input_payload,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return "smcl:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _serialize_response(self, response: SmallModelResponse) -> dict[str, Any]:
        return {
            "success": response.success,
            "parsed_json": response.parsed_json,
            "raw_text": response.raw_text,
            "provider_name": response.provider_name,
            "model_name": response.model_name,
            "input_tokens_estimate": response.input_tokens_estimate,
            "output_tokens_estimate": response.output_tokens_estimate,
            "latency_ms": response.latency_ms,
            "error_code": response.error_code,
            "error_message": response.error_message,
        }
