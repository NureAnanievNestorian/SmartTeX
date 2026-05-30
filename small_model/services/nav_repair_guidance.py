"""Small-model repair guidance for navigation / prepare_document_work.

This service is optional. Deterministic repair guidance must remain the
fallback. If the small model is disabled, quota is exhausted, or the provider
fails, callers should simply use deterministic repair.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from small_model import schemas
from small_model.services.base import SmallModelCallMixin
from small_model.services.payload import PayloadSanitizer
from small_model.task_types import FEATURE_NAV_REPAIR, TASK_NAV_REPAIR_GUIDANCE


_MAX_OPS = 8
_MAX_OP_TEXT = 800


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _truncate_patch_ops(ops: Any) -> list[dict[str, Any]]:
    if not isinstance(ops, list):
        return []

    out: list[dict[str, Any]] = []
    for raw in ops[:_MAX_OPS]:
        if not isinstance(raw, dict):
            continue

        item: dict[str, Any] = {}
        for key, value in raw.items():
            if isinstance(value, str):
                item[key] = value[:_MAX_OP_TEXT]
            elif isinstance(value, (int, float, bool)) or value is None:
                item[key] = value
            elif isinstance(value, list):
                item[key] = value[:20]
            elif isinstance(value, dict):
                item[key] = {
                    str(k): (v[:200] if isinstance(v, str) else v)
                    for k, v in list(value.items())[:20]
                }
            else:
                item[key] = str(value)[:200]

        out.append(item)

    return out


def _sanitize_repair_guidance(raw: dict[str, Any]) -> dict[str, Any]:
    error_kind = str(raw.get("error_kind") or "other").strip()
    valid_kinds = {
        "unknown_op",
        "include_required",
        "graph_error",
        "compile_error",
        "out_of_bounds",
        "stale_token",
        "malformed_include_file",
        "use_proposal_workflow",
        "other",
    }
    if error_kind not in valid_kinds:
        error_kind = "other"

    diagnosis = str(raw.get("diagnosis") or "").strip()[:240]

    fix_hint_raw = raw.get("fix_hint")
    fix_hint = fix_hint_raw if isinstance(fix_hint_raw, dict) else {}

    rewrite_op = fix_hint.get("rewrite_op")
    if rewrite_op is not None and not isinstance(rewrite_op, dict):
        rewrite_op = None

    add_op = fix_hint.get("add_op")
    if add_op is not None and not isinstance(add_op, dict):
        add_op = None

    additional_read_targets_raw = fix_hint.get("additional_read_targets")
    additional_read_targets = (
        additional_read_targets_raw[:5]
        if isinstance(additional_read_targets_raw, list)
        else []
    )

    return {
        "error_kind": error_kind,
        "diagnosis": diagnosis,
        "fix_hint": {
            "rewrite_op": rewrite_op,
            "add_op": add_op,
            "additional_read_targets": additional_read_targets,
        },
    }


class NavRepairGuidanceService(SmallModelCallMixin):
    """Generate optional repair guidance for failed validation attempts."""

    feature_key = FEATURE_NAV_REPAIR
    task_type = TASK_NAV_REPAIR_GUIDANCE

    def run(
        self,
        *,
        user,
        project,
        previous_error: dict[str, Any] | None,
        attempted_patch_ops: list[dict[str, Any]] | None,
        patch_op_schema_reminder: dict[str, Any] | None = None,
        deterministic_guidance: dict[str, Any] | None = None,
    ) -> Optional[dict[str, Any]]:
        enabled, _, _ = self.is_enabled(user, project)
        if not enabled:
            return None

        payload = PayloadSanitizer.clean_payload(
            {
                "previous_error": previous_error or {},
                "attempted_patch_ops": _truncate_patch_ops(attempted_patch_ops),
                "patch_op_schema_reminder": patch_op_schema_reminder or {},
                "deterministic_guidance": deterministic_guidance or {},
                "previous_error_hash": _stable_hash(previous_error or {}),
                "attempted_ops_hash": _stable_hash(attempted_patch_ops or []),
            }
        )

        response = self.call_provider(
            user=user,
            project=project,
            system_instruction=(
                "You help repair SmartTeX proposal validation failures. "
                "Return STRICT JSON only. Do not propose bypassing controlled mode. "
                "Prefer the existing deterministic guidance when it is already sufficient. "
                "Use only valid proposal ops from the provided schema reminder."
            ),
            input_payload=payload,
            response_schema=schemas.NAV_REPAIR_GUIDANCE_SCHEMA,
        )

        if response is None or not response.success or not response.parsed_json:
            if response is not None and response.error_code == "QUOTA_EXCEEDED":
                return {"_error": "QUOTA_EXCEEDED"}
            return None

        return _sanitize_repair_guidance(response.parsed_json)