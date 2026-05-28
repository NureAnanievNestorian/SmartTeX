from __future__ import annotations

from collections import Counter
from typing import Any

from small_model import schemas
from small_model.services.base import SmallModelCallMixin
from small_model.task_types import FEATURE_CIRCUIT_BREAKER, TASK_CIRCUIT_BREAKER_EVALUATE


class CircuitBreakerService(SmallModelCallMixin):
    feature_key = FEATURE_CIRCUIT_BREAKER
    task_type = TASK_CIRCUIT_BREAKER_EVALUATE

    def evaluate_deterministic(
        self,
        *,
        compile_failures: int = 0,
        tool_calls: list[str] | None = None,
        files_touched_total: int = 0,
        max_files: int = 1,
        diff_size_history: list[int] | None = None,
        rejected_patches: int = 0,
    ) -> dict[str, Any]:
        counts = Counter(tool_calls or [])
        if compile_failures >= 3:
            return self._stop("compile_failures_threshold")
        if counts and max(counts.values()) >= 3:
            return self._stop("repeated_tool_call_threshold")
        if files_touched_total > max_files * 1.5:
            return self._stop("files_touched_budget_exceeded")
        if rejected_patches >= 4:
            return self._stop("rejected_patch_threshold")
        history = diff_size_history or []
        if len(history) >= 3 and history[-1] >= history[-2] * 2 and history[-2] >= history[-3] * 2:
            return self._stop("diff_size_escalation")
        return {"decision": "continue", "reason": "", "deterministic": True}

    def evaluate(self, *, user, project, payload: dict[str, Any]) -> dict[str, Any]:
        deterministic = self.evaluate_deterministic(
            compile_failures=int(payload.get("compile_failures") or 0),
            tool_calls=list((payload.get("repeated_tool_calls") or {}).keys()),
            files_touched_total=int(payload.get("files_touched_total") or 0),
            max_files=int(payload.get("max_files") or 1),
            diff_size_history=payload.get("diff_size_history") or [],
            rejected_patches=int(payload.get("rejected_patches") or 0),
        )
        if deterministic["decision"] != "continue":
            return deterministic
        enabled, _, _ = self.is_enabled(user, project)
        if not enabled:
            return deterministic
        response = self.call_provider(
            user=user,
            project=project,
            system_instruction="Decide whether the edit attempt should continue, narrow scope, or stop.",
            input_payload=payload,
            response_schema=schemas.CIRCUIT_BREAKER_SCHEMA,
        )
        if response.success and response.parsed_json:
            return response.parsed_json
        streak = int(payload.get("smcl_unavailable_streak") or 0) + 1
        if streak >= 2:
            return {
                "decision": "narrow_scope",
                "reason": "small_model_unavailable_streak",
                "suggested_scope_reduction": "Retry with a smaller patch scope before attempting another compile fix.",
                "smcl_unavailable_streak": streak,
            }
        deterministic["smcl_unavailable_streak"] = streak
        return deterministic

    def _stop(self, reason: str) -> dict[str, Any]:
        return {"decision": "stop_and_ask_user", "reason": reason, "deterministic": True}
