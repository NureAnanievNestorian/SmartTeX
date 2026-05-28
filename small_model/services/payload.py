from __future__ import annotations

from typing import Any


class PayloadSanitizer:
    @staticmethod
    def trim_text(value: str, *, max_chars: int) -> str:
        text = str(value or "")
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n...TRUNCATED..."

    @staticmethod
    def trim_lines(value: str, *, max_lines: int) -> str:
        lines = str(value or "").splitlines()
        if len(lines) <= max_lines:
            return "\n".join(lines)
        return "\n".join(lines[:max_lines]) + "\n...TRUNCATED..."

    @staticmethod
    def clean_payload(payload: dict[str, Any]) -> dict[str, Any]:
        blocked_keys = {"api_key", "token", "password", "credential", "credentials", "secret"}
        return {key: value for key, value in payload.items() if key.lower() not in blocked_keys}
