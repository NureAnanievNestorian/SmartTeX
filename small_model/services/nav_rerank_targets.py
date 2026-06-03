"""Small-model reranker for ``prepare_document_work`` candidates.

Operates on top-N deterministically-prefiltered candidates. If the
provider fails or quota is exhausted, the caller MUST fall back to the
deterministic ordering (``indexed_keyword``).
"""
from __future__ import annotations

from typing import Any, Optional

from small_model import schemas
from small_model.services.base import SmallModelCallMixin
from small_model.services.payload import PayloadSanitizer
from small_model.task_types import FEATURE_NAV_RERANK, TASK_NAV_RERANK_TARGETS


_MAX_CANDIDATES = 8
_VALID_CONF = {"low", "medium", "high"}


def _sanitize(raw: dict, *, allowed_ids: set[str]) -> dict:
    ranked = raw.get("ranked") if isinstance(raw, dict) else None
    out: list[dict] = []
    if isinstance(ranked, list):
        for item in ranked:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("candidate_id") or "")
            if cid not in allowed_ids:
                continue
            conf = item.get("confidence")
            if conf not in _VALID_CONF:
                conf = "low"
            reason = str(item.get("reason") or "")[:200]
            out.append({"candidate_id": cid, "confidence": conf, "reason": reason})
    scope = raw.get("scope_confidence") if isinstance(raw, dict) else None
    if scope not in _VALID_CONF:
        scope = None
    return {"ranked": out, "scope_confidence": scope}


class NavRerankTargetsService(SmallModelCallMixin):
    feature_key = FEATURE_NAV_RERANK
    task_type = TASK_NAV_RERANK_TARGETS

    def run(
        self,
        *,
        user,
        project,
        user_request: str,
        candidates: list[dict],
    ) -> Optional[dict]:
        """Rerank candidates. Each candidate must include ``candidate_id``."""
        enabled, _, _ = self.is_enabled(user, project)
        if not enabled:
            return None
        if not candidates:
            return {"ranked": [], "scope_confidence": None}
        clamped = candidates[:_MAX_CANDIDATES]
        allowed_ids = {str(c.get("candidate_id")) for c in clamped}
        payload = PayloadSanitizer.clean_payload({
            "user_request": PayloadSanitizer.trim_text(user_request, max_chars=1200),
            "candidates": [
                {
                    "candidate_id": str(c.get("candidate_id")),
                    "filename": str(c.get("filename") or ""),
                    "region_title": str(c.get("region_title") or "")[:200],
                    "role": str(c.get("role") or ""),
                    "reachability": str(c.get("reachability") or ""),
                    "state": str(c.get("state") or ""),
                    "summary": str(c.get("summary") or "")[:280],
                    "reason": str(c.get("reason") or "")[:280],
                    "annotation_ids": list(c.get("annotation_ids") or [])[:20],
                    "annotation_instructions": [
                        {
                            "id": item.get("id"),
                            "status": str(item.get("status") or "")[:30],
                            "line_start": item.get("line_start"),
                            "line_end": item.get("line_end"),
                            "instruction": str(item.get("instruction") or "")[:220],
                            "selected_text": str(item.get("selected_text") or "")[:180],
                        }
                        for item in (c.get("annotation_instructions") or [])[:8]
                        if isinstance(item, dict)
                    ],
                    "deterministic_score": float(c.get("deterministic_score", 0.0) or 0.0),
                }
                for c in clamped
            ],
        })
        response = self.call_provider(
            user=user,
            project=project,
            system_instruction=(
                "Rerank candidates for the user's edit request. Return "
                "STRICT JSON. Only include candidate_ids from the input. "
                "Higher confidence means more likely to be the correct "
                "edit target. Do not invent paths. Treat annotation_ids and "
                "annotation_instructions as high-signal factual links to the "
                "target file/lines. deterministic_score is only a weak lexical "
                "signal; prefer concrete file summaries, headings, annotation "
                "context, and selected text when available."
            ),
            input_payload=payload,
            response_schema=schemas.NAV_RERANK_TARGETS_SCHEMA,
        )
        if response is None or not response.success or not response.parsed_json:
            if response is not None and response.error_code == "QUOTA_EXCEEDED":
                return {"_error": "QUOTA_EXCEEDED"}
            return None
        return _sanitize(response.parsed_json, allowed_ids=allowed_ids)
