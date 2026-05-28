from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class SmallModelResponse:
    success: bool
    parsed_json: dict[str, Any] | None = None
    raw_text: str | None = None
    provider_name: str = ""
    model_name: str = ""
    input_tokens_estimate: int = 0
    output_tokens_estimate: int = 0
    latency_ms: int = 0
    error_code: str | None = None
    error_message: str | None = None


class SmallModelProvider(ABC):
    provider_name = "base"

    @abstractmethod
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
        raise NotImplementedError


def estimate_tokens(value: Any, *, source_like: bool = True) -> int:
    text = value if isinstance(value, str) else repr(value)
    divisor = 3 if source_like else 4
    return max(1, (len(text) + divisor - 1) // divisor)
