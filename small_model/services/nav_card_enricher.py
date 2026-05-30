"""Small-model enrichment services for navigation FileCard / RegionCard."""
from __future__ import annotations

from typing import Any, Optional

from small_model import schemas
from small_model.services.base import SmallModelCallMixin
from small_model.services.payload import PayloadSanitizer
from small_model.task_types import (
    FEATURE_NAV_INDEX_ENRICH,
    TASK_NAV_FILE_CARD_ENRICH,
    TASK_NAV_REGION_CARD_ENRICH,
)


_VALID_STATES = {"real", "demo", "placeholder", "unknown"}
_VALID_CONF = {"low", "medium", "high"}
_VALID_ROLES = {
    "entrypoint", "content_section", "metadata", "style", "class",
    "bib", "csl", "config", "asset_metadata", "auxiliary", "unknown",
}
_DETERMINISTIC_ROLE_LOCKED = {
    "entrypoint", "bib", "csl", "class", "style", "asset_metadata",
}

_MAX_REP_LINES = 40
_MAX_REP_CHARS = 2000
_MAX_HEADINGS = 24
_MAX_TRIGGERS = 6


def _representative_slice(content: str) -> str:
    if not content:
        return ""
    text = PayloadSanitizer.trim_lines(content, max_lines=_MAX_REP_LINES)
    return PayloadSanitizer.trim_text(text, max_chars=_MAX_REP_CHARS)


def _clamp_triggers(items: Any) -> list[dict]:
    if not isinstance(items, list):
        return []

    out: list[dict] = []
    for item in items[:_MAX_TRIGGERS]:
        if not isinstance(item, dict):
            continue

        phrase = str(item.get("phrase") or "").strip()
        if not phrase or len(phrase) > 80:
            continue

        try:
            weight = float(item.get("weight", 1.0))
        except (TypeError, ValueError):
            weight = 1.0

        out.append(
            {
                "phrase": phrase[:80],
                "weight": max(0.0, min(weight, 5.0)),
            }
        )

    return out


def _sanitize_file_enrichment(raw: dict, *, deterministic_role: str) -> dict:
    summary = str(raw.get("summary") or "").strip()[:280]

    state = raw.get("state")
    if state not in _VALID_STATES:
        state = None

    state_confidence = raw.get("state_confidence")
    if state_confidence not in _VALID_CONF:
        state_confidence = "low"

    summary_confidence = raw.get("summary_confidence")
    if summary_confidence not in _VALID_CONF:
        summary_confidence = "low"

    role_refinement = raw.get("role_refinement")
    if (
        not isinstance(role_refinement, str)
        or role_refinement not in _VALID_ROLES
        or deterministic_role in _DETERMINISTIC_ROLE_LOCKED
        or deterministic_role not in {"unknown", "auxiliary"}
    ):
        role_refinement = None

    role_confidence = raw.get("role_confidence")
    if role_confidence not in _VALID_CONF:
        role_confidence = "low" if role_refinement else None

    return {
        "summary": summary,
        "state": state,
        "state_confidence": state_confidence,
        "summary_confidence": summary_confidence,
        "role_refinement": role_refinement,
        "role_confidence": role_confidence,
        "edit_triggers": _clamp_triggers(raw.get("edit_triggers")),
    }


def _sanitize_region_enrichment(raw: dict) -> dict:
    summary = str(raw.get("summary") or "").strip()[:280]

    state = raw.get("state")
    if state not in _VALID_STATES:
        state = None

    state_confidence = raw.get("state_confidence")
    if state_confidence not in _VALID_CONF:
        state_confidence = "low"

    summary_confidence = raw.get("summary_confidence")
    if summary_confidence not in _VALID_CONF:
        summary_confidence = "low"

    return {
        "summary": summary,
        "state": state,
        "state_confidence": state_confidence,
        "summary_confidence": summary_confidence,
        "edit_triggers": _clamp_triggers(raw.get("edit_triggers")),
    }


class NavFileCardEnrichService(SmallModelCallMixin):
    feature_key = FEATURE_NAV_INDEX_ENRICH
    task_type = TASK_NAV_FILE_CARD_ENRICH

    def run(
        self,
        *,
        user,
        project,
        filename: str,
        deterministic_role: str,
        deterministic_state: str,
        line_count: int,
        byte_size: int,
        heading_titles: list[str],
        representative_content: str,
        includes_out: list[str],
        included_by: list[str],
    ) -> Optional[dict]:
        enabled, _, _ = self.is_enabled(user, project)
        if not enabled:
            return None

        payload = PayloadSanitizer.clean_payload(
            {
                "filename": filename,
                "deterministic_role": deterministic_role,
                "deterministic_state": deterministic_state,
                "line_count": int(line_count or 0),
                "byte_size": int(byte_size or 0),
                "heading_titles": [str(t)[:160] for t in (heading_titles or [])[:_MAX_HEADINGS]],
                "representative_content": _representative_slice(representative_content),
                "includes_out": list(includes_out or [])[:24],
                "included_by": list(included_by or [])[:24],
            }
        )

        response = self.call_provider(
            user=user,
            project=project,
            system_instruction=(
                "Enrich a navigation FileCard. Return STRICT JSON. "
                "Do not invent files. Never override deterministic structural facts. "
                "Only refine role when deterministic_role is unknown/auxiliary."
            ),
            input_payload=payload,
            response_schema=schemas.NAV_FILE_CARD_ENRICH_SCHEMA,
        )

        if response is None or not response.success or not response.parsed_json:
            if response is not None and response.error_code == "QUOTA_EXCEEDED":
                return {"_error": "QUOTA_EXCEEDED"}
            return None

        return _sanitize_file_enrichment(
            response.parsed_json,
            deterministic_role=deterministic_role,
        )


class NavRegionCardEnrichService(SmallModelCallMixin):
    feature_key = FEATURE_NAV_INDEX_ENRICH
    task_type = TASK_NAV_REGION_CARD_ENRICH

    def run(
        self,
        *,
        user,
        project,
        filename: str,
        region_title: str,
        region_kind: str,
        line_start: int,
        line_end: int,
        deterministic_state: str,
        representative_content: str,
    ) -> Optional[dict]:
        enabled, _, _ = self.is_enabled(user, project)
        if not enabled:
            return None

        payload = PayloadSanitizer.clean_payload(
            {
                "filename": filename,
                "region_title": str(region_title or "")[:200],
                "region_kind": region_kind,
                "line_start": int(line_start or 0),
                "line_end": int(line_end or 0),
                "deterministic_state": deterministic_state,
                "representative_content": _representative_slice(representative_content),
            }
        )

        response = self.call_provider(
            user=user,
            project=project,
            system_instruction=(
                "Enrich a navigation RegionCard. Return STRICT JSON. "
                "Do not invent content. State must reflect what the body actually contains."
            ),
            input_payload=payload,
            response_schema=schemas.NAV_REGION_CARD_ENRICH_SCHEMA,
        )

        if response is None or not response.success or not response.parsed_json:
            if response is not None and response.error_code == "QUOTA_EXCEEDED":
                return {"_error": "QUOTA_EXCEEDED"}
            return None

        return _sanitize_region_enrichment(response.parsed_json)