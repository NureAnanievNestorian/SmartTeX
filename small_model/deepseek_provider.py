from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .provider import SmallModelProvider, SmallModelResponse, estimate_tokens

_RETRY_DELAYS = (2.0, 8.0)


class DeepSeekProvider(SmallModelProvider):
    provider_name = "deepseek"

    def __init__(self, api_key: str | None = None, model_name: str | None = None):
        self.api_key = api_key if api_key is not None else str(getattr(settings, "DEEPSEEK_API_KEY", "")).strip()
        self.model_name = model_name or str(getattr(settings, "DEEPSEEK_SMALL_MODEL_NAME", "deepseek-v4-flash"))
        if not self.api_key:
            raise ImproperlyConfigured("DEEPSEEK_API_KEY is required when SMALL_MODEL_PROVIDER='deepseek'.")

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
        started = time.monotonic()
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": self._build_system_instruction(system_instruction, response_schema)},
                {"role": "user", "content": json.dumps(input_payload, ensure_ascii=False)},
            ],
            "stream": False,
            "temperature": float(getattr(settings, "DEEPSEEK_TEMPERATURE", 0)),
            "max_tokens": int(getattr(settings, "DEEPSEEK_MAX_OUTPUT_TOKENS", 1024)),
            "response_format": {"type": "json_object"},
        }
        top_p = getattr(settings, "DEEPSEEK_TOP_P", None)
        if top_p is not None:
            payload["top_p"] = top_p
        thinking_type = str(getattr(settings, "DEEPSEEK_THINKING_TYPE", "disabled") or "").strip().lower()
        if thinking_type in {"enabled", "disabled"}:
            payload["thinking"] = {"type": thinking_type}
        reasoning_effort = str(getattr(settings, "DEEPSEEK_REASONING_EFFORT", "") or "").strip().lower()
        if reasoning_effort and thinking_type == "enabled":
            payload["reasoning_effort"] = reasoning_effort
        request = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        for delay in (*_RETRY_DELAYS, None):
            try:
                with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                    body = response.read().decode("utf-8", errors="replace")
                data = json.loads(body)
                raw_text = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                usage = data.get("usage") or {}
                try:
                    parsed = json.loads(raw_text)
                except json.JSONDecodeError:
                    parsed = self._try_repair_json_object(raw_text)
                    if parsed is None:
                        return self._error(
                            "INVALID_JSON",
                            "Provider returned non-JSON text.",
                            started,
                            raw_text=raw_text,
                            input_tokens=int(usage.get("prompt_tokens") or estimate_tokens(input_payload)),
                            output_tokens=int(usage.get("completion_tokens") or estimate_tokens(raw_text, source_like=False)),
                        )
                return SmallModelResponse(
                    success=True,
                    parsed_json=parsed,
                    raw_text=raw_text,
                    provider_name=self.provider_name,
                    model_name=str(data.get("model") or self.model_name),
                    input_tokens_estimate=int(usage.get("prompt_tokens") or estimate_tokens(input_payload)),
                    output_tokens_estimate=int(usage.get("completion_tokens") or estimate_tokens(raw_text, source_like=False)),
                    latency_ms=self._latency(started),
                )
            except TimeoutError:
                return self._error("TIMEOUT", "Provider request timed out.", started)
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and delay is not None:
                    time.sleep(delay)
                    continue
                if exc.code == 429:
                    return self._error("PROVIDER_RATE_LIMITED", "Provider rate limit exceeded.", started)
                return self._error("PROVIDER_ERROR", f"Provider HTTP error {exc.code}.", started)
            except json.JSONDecodeError:
                return self._error("INVALID_JSON", "Provider envelope was not JSON.", started)
            except Exception as exc:
                return self._error("PROVIDER_ERROR", str(exc), started)
        return self._error("PROVIDER_RATE_LIMITED", "Provider rate limit exceeded after retries.", started)

    def _build_system_instruction(self, system_instruction: str, response_schema: dict[str, Any]) -> str:
        schema_json = json.dumps(response_schema, ensure_ascii=False, sort_keys=True)
        return (
            f"{system_instruction}\n\n"
            "Return only valid JSON matching this schema. Do not wrap it in markdown.\n"
            f"JSON schema:\n{schema_json}"
        )

    def _latency(self, started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    def _error(
        self,
        code: str,
        message: str,
        started: float,
        *,
        raw_text: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> SmallModelResponse:
        return SmallModelResponse(
            success=False,
            raw_text=raw_text,
            provider_name=self.provider_name,
            model_name=self.model_name,
            input_tokens_estimate=max(0, int(input_tokens or 0)),
            output_tokens_estimate=max(0, int(output_tokens or 0)),
            latency_ms=self._latency(started),
            error_code=code,
            error_message=message[:500],
        )

    def _try_repair_json_object(self, raw_text: str) -> dict[str, Any] | None:
        text = str(raw_text or "").strip()
        if not text.startswith("{"):
            return None
        fixed = self._close_truncated_json(text)
        if fixed is None:
            return None
        try:
            parsed = json.loads(fixed)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _close_truncated_json(self, text: str) -> str | None:
        closers: list[str] = []
        in_string = False
        escape = False
        last_structural = -1
        for idx, ch in enumerate(text):
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch == "{":
                closers.append("}")
                last_structural = idx
            elif ch == "[":
                closers.append("]")
                last_structural = idx
            elif ch in "}]":
                if not closers or closers[-1] != ch:
                    return None
                closers.pop()
                last_structural = idx
            elif ch == ",":
                last_structural = idx - 1
        trimmed = text.rstrip()
        if in_string:
            trimmed += '"'
        trimmed = trimmed.rstrip()
        if trimmed.endswith(","):
            trimmed = trimmed[:-1].rstrip()
        elif last_structural >= 0 and last_structural < len(trimmed) - 1:
            suffix = trimmed[last_structural + 1:].strip()
            if suffix == ",":
                trimmed = trimmed[:last_structural + 1].rstrip()
        return trimmed + "".join(reversed(closers))
