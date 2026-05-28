from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

_RETRY_DELAYS = (2.0, 8.0)  # seconds to wait before 1st and 2nd retry on 429

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .provider import SmallModelProvider, SmallModelResponse, estimate_tokens


class GeminiProvider(SmallModelProvider):
    provider_name = "gemini"

    def __init__(self, api_key: str | None = None, model_name: str | None = None):
        self.api_key = api_key if api_key is not None else str(getattr(settings, "GEMINI_API_KEY", "")).strip()
        self.model_name = model_name or str(getattr(settings, "GEMINI_SMALL_MODEL_NAME", "gemini-2.0-flash-lite"))
        if not self.api_key:
            raise ImproperlyConfigured("GEMINI_API_KEY is required when SMALL_MODEL_PROVIDER='gemini'.")

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
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model_name}:generateContent?key={self.api_key}"
        )
        payload: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system_instruction}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps(input_payload, ensure_ascii=False)}]}],
            "generationConfig": {
                "temperature": float(getattr(settings, "GEMINI_TEMPERATURE", 0)),
                "maxOutputTokens": int(getattr(settings, "GEMINI_MAX_OUTPUT_TOKENS", 1024)),
                "responseMimeType": "application/json",
                "responseSchema": response_schema,
            },
        }
        top_p = getattr(settings, "GEMINI_TOP_P", None)
        if top_p is not None:
            payload["generationConfig"]["topP"] = top_p
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            try:
                with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                    body = response.read().decode("utf-8", errors="replace")
                data = json.loads(body)
                raw_text = (
                    data.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                )
                try:
                    parsed = json.loads(raw_text)
                except json.JSONDecodeError:
                    return self._error("INVALID_JSON", "Provider returned non-JSON text.", started, raw_text=raw_text)
                usage = data.get("usageMetadata") or {}
                return SmallModelResponse(
                    success=True,
                    parsed_json=parsed,
                    raw_text=raw_text,
                    provider_name=self.provider_name,
                    model_name=self.model_name,
                    input_tokens_estimate=int(usage.get("promptTokenCount") or estimate_tokens(input_payload)),
                    output_tokens_estimate=int(usage.get("candidatesTokenCount") or estimate_tokens(raw_text, source_like=False)),
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

    def _latency(self, started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    def _error(self, code: str, message: str, started: float, *, raw_text: str | None = None) -> SmallModelResponse:
        return SmallModelResponse(
            success=False,
            raw_text=raw_text,
            provider_name=self.provider_name,
            model_name=self.model_name,
            input_tokens_estimate=0,
            output_tokens_estimate=0,
            latency_ms=self._latency(started),
            error_code=code,
            error_message=message[:500],
        )
