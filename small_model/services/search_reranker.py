"""Small-model reranker for Smart Search results.

Operates on top-N deterministically pre-filtered candidates. Falls back
gracefully if the provider is unavailable or quota is exhausted.
"""
from __future__ import annotations

from typing import Any, Optional

from small_model import schemas
from small_model.services.base import SmallModelCallMixin
from small_model.services.payload import PayloadSanitizer
from small_model.task_types import FEATURE_NAV_RERANK, TASK_NAV_SEARCH_RERANK

_MAX_CANDIDATES = 20
_VALID_CONF = frozenset({"low", "medium", "high"})
_VALID_MATCH_KINDS = frozenset({
    "exact_match", "semantic_match", "related_context", "possible_conflict",
    "placeholder_or_demo", "old_topic_residue", "citation_or_source",
    "diagram_reference", "definition",
})


def _sanitize(raw: Any, *, allowed_ids: set[str]) -> dict:
    ranked: list[dict] = []
    if isinstance(raw, dict):
        items = raw.get("ranked")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                cid = str(item.get("candidate_id") or "")
                if cid not in allowed_ids:
                    continue
                conf = item.get("confidence")
                if conf not in _VALID_CONF:
                    conf = "low"
                mk = item.get("match_kind")
                if mk not in _VALID_MATCH_KINDS:
                    mk = None
                reason = str(item.get("reason") or "")[:300]
                ranked.append({"candidate_id": cid, "confidence": conf, "match_kind": mk, "reason": reason})
    return {"ranked": ranked}


class SearchRerankerService(SmallModelCallMixin):
    feature_key = FEATURE_NAV_RERANK
    task_type = TASK_NAV_SEARCH_RERANK

    def run(
        self,
        *,
        user,
        project,
        query: str,
        candidates: list[dict],
    ) -> Optional[dict]:
        enabled, _, _ = self.is_enabled(user, project)
        if not enabled:
            return None
        if not candidates:
            return {"ranked": []}
        clamped = candidates[:_MAX_CANDIDATES]
        allowed_ids = {str(c.get("candidate_id")) for c in clamped}
        payload = PayloadSanitizer.clean_payload({
            "query": PayloadSanitizer.trim_text(query, max_chars=800),
            "candidates": [
                {
                    "candidate_id": str(c.get("candidate_id")),
                    "filename": str(c.get("filename") or ""),
                    "region_title": str(c.get("region_title") or "")[:200],
                    "match_kind": str(c.get("match_kind") or ""),
                    "confidence": str(c.get("confidence") or ""),
                    "reason": str(c.get("reason") or "")[:200],
                    "snippet": str(c.get("snippet") or "")[:200],
                }
                for c in clamped
            ],
        })
        response = self.call_provider(
            user=user,
            project=project,
            system_instruction=(
                "Rerank these document search results for the user's query. "
                "Return STRICT JSON with a 'ranked' array. For each candidate, "
                "classify match_kind from: exact_match, semantic_match, "
                "related_context, possible_conflict, placeholder_or_demo, "
                "old_topic_residue, citation_or_source, diagram_reference, definition. "
                "Set confidence (low/medium/high) and a short reason. "
                "Only use candidate_ids from the input. Do not invent filenames."
            ),
            input_payload=payload,
            response_schema=schemas.SEARCH_RERANK_SCHEMA,
        )
        if response is None or not response.success or not response.parsed_json:
            if response is not None and response.error_code == "QUOTA_EXCEEDED":
                return {"_error": "QUOTA_EXCEEDED"}
            return None
        return _sanitize(response.parsed_json, allowed_ids=allowed_ids)
