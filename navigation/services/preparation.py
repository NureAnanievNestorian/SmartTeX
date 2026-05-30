"""``prepare_document_work`` service entry-point.

Read/preparation only: this module never edits user content, never
creates/modifies ``ChangeProposal`` or ``AISession``, and never holds
proposal locks. It MAY write navigation index rows (the index is
server-owned bookkeeping) and MAY write the preparation-result cache.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from django.utils import timezone

from projects.models import Project
from projects.services import main_source_filename, project_dir

from ..models import (
    Confidence,
    FileCard,
    FileRole,
    IndexStatus,
    NAV_SCHEMA_VERSION,
    ProjectNavigationIndex,
    Reachability,
    RegionCard,
    StateKind,
)
from . import cache as prep_cache
from . import freshness as fr
from . import mode_selector as sel
from .index_builder import build_navigation_index
from .repair import build_repair_guidance, patch_op_schema_reminder

logger = logging.getLogger(__name__)


def _canon_graph_path(value: str | None) -> str:
    if not value:
        return ""
    raw = str(value).replace("\\", "/")
    normalized = posixpath.normpath(raw)
    if normalized == ".":
        return ""
    return normalized.lstrip("./")


_FRESH_TTL_SECONDS_DEFAULT = 600
_MAX_READ_TARGETS = 6
_MAX_EDIT_TARGETS = 4

_TASK_TYPE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("compile_fix", re.compile(r"\b(compile|bibliography|\.bib|fix|error|помилк|виправ)\b", re.I)),
    ("new_section", re.compile(r"\b(add (a |new )?section|create section|add chapter|new chapter)\b", re.I)),
    ("section_edit", re.compile(r"\b(rewrite|restructure|переписати|refactor|move section)\b", re.I)),
    ("micro_edit", re.compile(r"\b(typo|rename|change word|fix word|change title)\b", re.I)),
    ("review_only", re.compile(r"\b(explain|describe|what does|review|analyze|analyse)\b", re.I)),
    ("paragraph_edit", re.compile(r"\b(fill in|write|expand|replace paragraph|update paragraph|update intro|fill out)\b", re.I)),
]


def _infer_task_type(text: str) -> str:
    if not text:
        return "unknown"
    for kind, pat in _TASK_TYPE_PATTERNS:
        if pat.search(text):
            return kind
    return "unknown"


def _edit_budget(task_type: str) -> tuple[int, int]:
    return {
        "micro_edit": (5, 1),
        "paragraph_edit": (40, 2),
        "section_edit": (150, 3),
        "new_section": (200, 3),
        "compile_fix": (40, 3),
        "review_only": (0, 0),
        "unknown": (50, 3),
    }.get(task_type, (50, 3))


def _empty_response() -> dict[str, Any]:
    return {
        "preparation_id": "",
        "mode": "minimal",
        "issued_at": timezone.now().isoformat(),
        "fresh_until_seconds": _FRESH_TTL_SECONDS_DEFAULT,
        "reuse_count": 0,
        "capabilities": {
            "navigation_index": "absent",
            "small_model_enrich": "n/a",
            "small_model_rerank": "n/a",
            "small_model_repair": "n/a",
            "longdoc": "unknown",
            "controlled_mcp_mode": False,
        },
        "project_brief": {},
        "task_type": "unknown",
        "scope_confidence": "low",
        "read_targets": [],
        "likely_edit_targets": [],
        "constraints": {
            "edit_mode": "unknown",
            "max_changed_lines": 50,
            "max_files": 3,
            "do_not_touch_files": [],
            "do_not_touch_region_card_ids": [],
            "relevant_requirements": [],
            "relevant_summaries": [],
        },
        "patch_op_schema_reminder": patch_op_schema_reminder(),
        "warnings": [],
        "do_not": [],
        "fallback_structure": None,
        "repair_guidance": None,
        # Internal anchors used by freshness/cache; kept in the payload so
        # cache reuse can validate them. Not part of the public contract.
        "schema_version": NAV_SCHEMA_VERSION,
        "base_version_number": 0,
        "markup_type_snapshot": "",
        "main_file_snapshot": "",
    }


def _project_brief(project: Project, index: Optional[ProjectNavigationIndex]) -> dict[str, Any]:
    main_file = main_source_filename(project)
    file_count = index.file_cards.filter(reachability=Reachability.REACHABLE).count() if index else 0
    return {
        "markup_type": project.markup_type,
        "entrypoint": main_file,
        "structure_hint": (
            f"{project.markup_type} project, {file_count} reachable source file(s)"
            if file_count else f"{project.markup_type} project"
        ),
        "main_file_changed_since_build": bool(
            index and (index.main_file_snapshot or "") != main_file
        ),
    }


def _capabilities(
    *,
    project: Project,
    index: Optional[ProjectNavigationIndex],
    index_status: str,
    controlled: bool,
) -> dict[str, Any]:
    nav_state = "absent"
    if index:
        nav_state = {
            "current": "ready",
            "partial_stale": "stale",
            "whole_invalid": "stale",
            "absent": "absent",
            "failed": "failed",
            "building": "building",
        }.get(index_status, "ready")
    enrich, rerank, repair = _small_model_capability_flags(project)
    return {
        "navigation_index": nav_state,
        "small_model_enrich": enrich,
        "small_model_rerank": rerank,
        "small_model_repair": repair,
        "longdoc": "unknown",
        "controlled_mcp_mode": controlled,
    }


def _small_model_capability_flags(project: Project) -> tuple[str, str, str]:
    """Return (enrich, rerank, repair) capability strings."""
    try:
        from small_model.services.nav_card_enricher import NavFileCardEnrichService
        from small_model.services.nav_rerank_targets import NavRerankTargetsService
        from small_model.services.nav_repair_guidance import NavRepairGuidanceService
    except Exception:  # pragma: no cover
        return "n/a", "n/a", "n/a"
    owner = getattr(project, "owner", None)
    if owner is None:
        return "n/a", "n/a", "n/a"

    def _flag(service) -> str:
        try:
            enabled, _, _ = service.is_enabled(owner, project)
        except Exception:
            return "error"
        return "active" if enabled else "disabled"

    return (
        _flag(NavFileCardEnrichService()),
        _flag(NavRerankTargetsService()),
        _flag(NavRepairGuidanceService()),
    )


def _file_card_target(
    card: FileCard,
    *,
    reason: str,
    confidence: str,
    region: Optional[RegionCard] = None,
    kind: str = "file",
    suggested_tool: str = "read_file_lines",
) -> dict[str, Any]:
    line_start = region.line_start if region else 1
    line_end = region.line_end if region else max(1, card.line_count or 1)
    return {
        "filename": card.filename,
        "line_start": int(line_start),
        "line_end": int(line_end),
        "region_card_id": region.id if region else None,
        "region_title": (region.title if region else None),
        "kind": kind if not region else "region",
        "state": (region.state if region else card.state),
        "reason": reason,
        "confidence": confidence,
        "suggested_tool": suggested_tool,
    }


# --- mode implementations ---------------------------------------------------


def _populate_cheap_direct(
    payload: dict[str, Any],
    *,
    hit: dict[str, Any],
    user_request: str,
) -> None:
    file_card: FileCard = hit["file_card"]
    region_card: Optional[RegionCard] = hit.get("region_card")
    task_type = _infer_task_type(user_request)
    payload["task_type"] = task_type
    max_lines, max_files = _edit_budget(task_type)
    payload["scope_confidence"] = "high"
    target = _file_card_target(
        file_card,
        reason=f"Triggered by {hit.get('trigger', 'narrow rule')}",
        confidence="high",
        region=region_card,
    )
    payload["read_targets"] = [target]
    payload["likely_edit_targets"] = [
        {
            "filename": file_card.filename,
            "line_start": target["line_start"],
            "line_end": target["line_end"],
            "region_card_id": region_card.id if region_card else None,
            "region_title": region_card.title if region_card else "",
            "state": region_card.state if region_card else file_card.state,
            "reason": target["reason"],
            "confidence": "high",
        }
    ]
    payload["constraints"]["edit_mode"] = task_type
    payload["constraints"]["max_changed_lines"] = max_lines
    payload["constraints"]["max_files"] = max_files


_KEYWORD_RE = re.compile(r"[A-Za-zА-Яа-яЇїІіЄєҐґ0-9]{3,}")


def _request_keywords(user_request: str) -> set[str]:
    words = _KEYWORD_RE.findall(user_request or "")
    return {w.lower() for w in words if len(w) >= 3}


def _score_file_card(card: FileCard, keywords: set[str]) -> float:
    if not keywords:
        return 0.0
    score = 0.0
    # Filename / stem overlap.
    name = Path(card.filename).stem.lower()
    if name in keywords:
        score += 4.0
    for token in re.split(r"[\-_./]", name):
        if token and token in keywords:
            score += 1.0
    # Triggers.
    for trig in card.edit_triggers or []:
        phrase = str((trig or {}).get("phrase", "")).lower()
        if not phrase:
            continue
        if phrase in keywords:
            score += 2.0
        elif any(p in phrase or phrase in p for p in keywords):
            score += 0.5
    # Role priors.
    if card.role == FileRole.ENTRYPOINT:
        score += 0.5
    if card.role in {FileRole.CONTENT_SECTION, FileRole.METADATA}:
        score += 0.3
    # State / reachability adjustments.
    if card.reachability == Reachability.REACHABLE:
        score += 0.5
    if card.reachability in {Reachability.MISSING, Reachability.EXCLUDED}:
        score -= 5.0
    if card.is_stale:
        score -= 0.5
    return score


def _score_region(region: RegionCard, keywords: set[str]) -> float:
    if not keywords:
        return 0.0
    score = 0.0
    title = (region.title or "").lower()
    if title and title in " ".join(keywords):
        score += 2.0
    for token in re.split(r"[\-_./\s]+", title):
        if token and token in keywords:
            score += 1.5
    for trig in region.edit_triggers or []:
        phrase = str((trig or {}).get("phrase", "")).lower()
        if phrase and phrase in keywords:
            score += 2.0
    if region.is_stale:
        score -= 0.5
    if region.state == StateKind.PLACEHOLDER:
        score += 0.7
    if region.state == StateKind.DEMO:
        score += 0.5
    return score


def _populate_indexed_keyword(
    payload: dict[str, Any],
    *,
    index: ProjectNavigationIndex,
    user_request: str,
) -> None:
    keywords = _request_keywords(user_request)
    task_type = _infer_task_type(user_request)
    payload["task_type"] = task_type
    max_lines, max_files = _edit_budget(task_type)
    payload["constraints"]["edit_mode"] = task_type
    payload["constraints"]["max_changed_lines"] = max_lines
    payload["constraints"]["max_files"] = max_files

    file_cards = list(
        index.file_cards.exclude(reachability=Reachability.EXCLUDED)
        .exclude(reachability=Reachability.MISSING)
    )
    scored = [
        (_score_file_card(fc, keywords), fc) for fc in file_cards
    ]
    scored = [s for s in scored if s[0] > 0]
    scored.sort(key=lambda s: s[0], reverse=True)

    read_targets: list[dict[str, Any]] = []
    edit_targets: list[dict[str, Any]] = []
    do_not_touch: list[str] = []
    seen_files: set[str] = set()

    for score, fc in scored[:_MAX_READ_TARGETS]:
        confidence = "high" if score >= 4 else ("medium" if score >= 2 else "low")
        if fc.is_stale:
            payload["warnings"].append(f"card_stale:{fc.filename}")
            confidence = "low"
        # Best region within this file.
        best_region: Optional[RegionCard] = None
        best_region_score = -1.0
        for region in fc.region_cards.all():
            rs = _score_region(region, keywords)
            if rs > best_region_score:
                best_region_score = rs
                best_region = region
        target = _file_card_target(
            fc,
            reason=f"keyword match score={score:.1f}",
            confidence=confidence,
            region=best_region if best_region_score > 0 else None,
            suggested_tool="read_file_lines",
        )
        read_targets.append(target)
        if fc.role not in {FileRole.BIB, FileRole.CSL, FileRole.STYLE, FileRole.CLASS} and len(edit_targets) < _MAX_EDIT_TARGETS:
            edit_targets.append({
                "filename": fc.filename,
                "line_start": target["line_start"],
                "line_end": target["line_end"],
                "region_card_id": target["region_card_id"],
                "region_title": (best_region.title if best_region else ""),
                "state": (best_region.state if best_region else fc.state),
                "reason": target["reason"],
                "confidence": confidence,
            })
        seen_files.add(fc.filename)

    # Surface excluded / read-only cards as do_not_touch.
    for fc in index.file_cards.filter(reachability=Reachability.EXCLUDED):
        do_not_touch.append(fc.filename)

    payload["read_targets"] = read_targets
    payload["likely_edit_targets"] = edit_targets
    payload["constraints"]["do_not_touch_files"] = do_not_touch

    if not read_targets:
        payload["scope_confidence"] = "low"
        payload["warnings"].append("no_keyword_match")
    elif scored and scored[0][0] >= 4:
        payload["scope_confidence"] = "high"
    else:
        payload["scope_confidence"] = "medium"


def _populate_fallback_structural(
    payload: dict[str, Any],
    *,
    project: Project,
) -> None:
    payload["task_type"] = _infer_task_type(payload.get("_user_request", "")) or "unknown"
    try:
        from longdoc.document_graph import inspect_document_graph
        graph = inspect_document_graph(project)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("document_graph inspection failed: %s", exc)
        graph = None

    root = project_dir(project)
    inventory: list[str] = []
    if root.exists():
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = _canon_graph_path(path.relative_to(root).as_posix())
            if rel.startswith(".git/") or "/.smarttex" in rel:
                continue
            inventory.append(rel)
            if len(inventory) >= 200:
                break

    payload["fallback_structure"] = {
        "entrypoint": _canon_graph_path(main_source_filename(project)),
        "reachable_files": [_canon_graph_path(p) for p in (getattr(graph, "reachable_files", None) or [])] if graph else [],
        "orphan_files": [_canon_graph_path(p) for p in (getattr(graph, "orphan_source_files", None) or [])] if graph else [],
        "missing_files": [_canon_graph_path(p) for p in (getattr(graph, "missing_files", []) or [])] if graph else [],
        "file_inventory": inventory,
    }
    payload["scope_confidence"] = "low"
    payload["warnings"].append("index_unavailable_using_structural_fallback")

    reachable_files = [_canon_graph_path(p) for p in (getattr(graph, "reachable_files", None) or [])] if graph else []
    if reachable_files:
        payload["read_targets"] = [
            {
                "filename": fn,
                "line_start": 1,
                "line_end": 200,
                "region_card_id": None,
                "kind": "file",
                "reason": "reachable source file (fallback)",
                "confidence": "low",
                "suggested_tool": "read_file_lines",
            }
            for fn in reachable_files[:_MAX_READ_TARGETS]
        ]


def _populate_minimal(payload: dict[str, Any], *, reasons: list[str]) -> None:
    payload["warnings"].extend(reasons or ["index_unavailable"])
    payload["scope_confidence"] = "low"
    payload["do_not"].extend([
        "Do not edit files you have not first read.",
        "Do not bypass USE_PROPOSAL_WORKFLOW with import_project_zip.",
    ])


# --- public entry-point -----------------------------------------------------


def prepare_document_work(
    project: Project,
    *,
    user_request: str,
    preparation_id: Optional[str] = None,
    previous_error: Optional[dict] = None,
    attempted_patch_ops: Optional[list[dict]] = None,
    selected_file: Optional[str] = None,
    selected_region_id: Optional[int] = None,
) -> dict[str, Any]:
    """Build a preparation response for ``user_request``.

    Never raises: internal failures degrade to ``mode='minimal'`` with
    descriptive warnings.
    """
    import time as _time
    _t0 = _time.monotonic()

    response = _empty_response()
    response["_user_request"] = user_request or ""


    try:
        # --- cache hit fast path -------------------------------------------------
        if preparation_id:
            cached = prep_cache.lookup_by_id(preparation_id)
            try:
                index = ProjectNavigationIndex.objects.get(project=project)
            except ProjectNavigationIndex.DoesNotExist:
                index = None
            if (
                cached
                and prep_cache.is_reusable(cached)
                and fr.preparation_is_fresh(cached, project=project, index=index)
            ):
                bumped = prep_cache.bump_reuse(cached)
                bumped.pop("_user_request", None)
                return bumped

        # --- evaluate freshness --------------------------------------------------
        freshness = fr.evaluate_index(project)

        # If index is absent or build was never attempted, trigger a build
        # synchronously so first-call works. Bounded by builder itself.
        index_obj: Optional[ProjectNavigationIndex] = None
        if freshness.status == "absent":
            try:
                build_navigation_index(project, use_small_model=False)
                freshness = fr.evaluate_index(project)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("inline navigation index build failed: %s", exc)
                response["warnings"].append("index_build_failed")
        try:
            index_obj = ProjectNavigationIndex.objects.get(project=project)
        except ProjectNavigationIndex.DoesNotExist:
            index_obj = None

        graph_available = False
        try:
            from longdoc.document_graph import inspect_document_graph
            graph_available = bool(inspect_document_graph(project))
        except Exception:
            graph_available = False

        inputs = sel.SelectionInputs(
            user_request=user_request or "",
            preparation_id=preparation_id,
            previous_error=previous_error,
            attempted_patch_ops=attempted_patch_ops,
            selected_file=selected_file,
            selected_region_id=selected_region_id,
        )
        mode = sel.select_mode(
            inputs,
            index=index_obj,
            index_freshness_status=freshness.status,
            graph_available=graph_available,
        )

        # Cache lookup by request signature for non-repair, non-cheap-direct flows.
        if mode in {"indexed_keyword", "fallback_structural"} and index_obj:
            cached = prep_cache.lookup_by_request(
                project_id=project.id,
                user_request=user_request or "",
                schema_version=index_obj.schema_version,
                version_number=index_obj.last_built_version_number,
            )
            if (
                cached
                and prep_cache.is_reusable(cached)
                and fr.preparation_is_fresh(cached, project=project, index=index_obj)
            ):
                bumped = prep_cache.bump_reuse(cached)
                bumped.pop("_user_request", None)
                return bumped

        # --- populate response ---------------------------------------------------
        response["preparation_id"] = prep_cache.new_preparation_id()
        response["issued_at"] = timezone.now().isoformat()
        response["mode"] = mode
        response["project_brief"] = _project_brief(project, index_obj)
        response["capabilities"] = _capabilities(
            project=project,
            index=index_obj,
            index_status=freshness.status,
            controlled=False,
        )
        if index_obj:
            response["schema_version"] = int(index_obj.schema_version)
            response["base_version_number"] = int(index_obj.last_built_version_number)
            response["markup_type_snapshot"] = index_obj.markup_type_snapshot or ""
            response["main_file_snapshot"] = index_obj.main_file_snapshot or ""

        if freshness.reasons and mode != "repair":
            for r in freshness.reasons:
                response["warnings"].append(r)

        if mode == "repair":
            guidance, read_targets, warnings = build_repair_guidance(
                previous_error=previous_error,
                attempted_patch_ops=attempted_patch_ops,
            )
            # Optional small-model augmentation (additive, never authoritative).
            sm_repair = _maybe_small_model_repair(
                project=project,
                previous_error=previous_error,
                attempted_patch_ops=attempted_patch_ops,
                deterministic_guidance=guidance,
                response=response,
            )
            if sm_repair:
                guidance = _merge_repair_guidance(guidance, sm_repair)
            response["repair_guidance"] = guidance
            response["read_targets"] = read_targets
            response["warnings"].extend(warnings)
            response["scope_confidence"] = "medium"
            response["task_type"] = _infer_task_type(user_request or "") or "compile_fix"

        elif mode == "cheap_direct" and index_obj:
            hit = sel.cheap_direct_match(inputs, index_obj)
            if hit:
                _populate_cheap_direct(response, hit=hit, user_request=user_request or "")
            else:  # safety net
                response["mode"] = "minimal"
                _populate_minimal(response, reasons=["cheap_direct_unmatched"])

        elif mode == "indexed_keyword" and index_obj:
            _populate_indexed_keyword(
                response, index=index_obj, user_request=user_request or ""
            )
            # Optional small-model rerank promotion.
            promoted = _maybe_small_model_rerank(
                project=project,
                user_request=user_request or "",
                response=response,
            )
            if promoted:
                response["mode"] = "indexed_reranked"

        elif mode == "fallback_structural":
            _populate_fallback_structural(response, project=project)

        elif mode == "minimal":
            _populate_minimal(response, reasons=freshness.reasons)

        # Cacheable modes — store the response.
        cacheable_modes = {"indexed_keyword", "indexed_reranked", "fallback_structural", "cheap_direct"}
        if response["mode"] in cacheable_modes and index_obj:
            try:
                prep_cache.store(
                    payload=response,
                    project_id=project.id,
                    user_request=user_request or "",
                    schema_version=index_obj.schema_version,
                    version_number=index_obj.last_built_version_number,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("preparation cache write failed: %s", exc)

    except Exception as exc:
        logger.exception("prepare_document_work degraded to minimal: %s", exc)
        response = _empty_response()
        response["preparation_id"] = prep_cache.new_preparation_id()
        response["issued_at"] = timezone.now().isoformat()
        response["mode"] = "minimal"
        response["warnings"].append(f"internal_error:{type(exc).__name__}")
        _populate_minimal(response, reasons=["internal_error"])

    response.pop("_user_request", None)
    try:
        logger.info(
            "nav.prepare project=%s mode=%s scope=%s latency_ms=%.1f caps=%s warnings=%s",
            project.id,
            response.get("mode"),
            response.get("scope_confidence"),
            (_time.monotonic() - _t0) * 1000.0,
            response.get("capabilities") or {},
            response.get("warnings") or [],
        )
    except Exception:  # pragma: no cover
        pass
    return response


# --- small-model layer ------------------------------------------------------


def _maybe_small_model_rerank(
    *, project: Project, user_request: str, response: dict[str, Any]
) -> bool:
    """Apply small-model rerank to ``response['read_targets']`` in place.

    Returns True iff the rerank actually ran and changed the ordering /
    confidence (caller promotes mode to ``indexed_reranked``).
    """
    try:
        from small_model.services.nav_rerank_targets import NavRerankTargetsService
    except Exception:  # pragma: no cover
        return False
    owner = getattr(project, "owner", None)
    if owner is None:
        return False
    service = NavRerankTargetsService()
    try:
        enabled, _, _ = service.is_enabled(owner, project)
    except Exception:
        enabled = False
    if not enabled:
        return False
    read_targets = response.get("read_targets") or []
    if len(read_targets) < 2:
        return False
    candidates = []
    for idx, t in enumerate(read_targets):
        candidates.append({
            "candidate_id": f"c{idx}",
            "filename": t.get("filename"),
            "region_title": t.get("region_title") or "",
            "role": "",
            "state": t.get("state") or "",
            "summary": t.get("reason") or "",
            "deterministic_score": float(len(read_targets) - idx),
        })
    try:
        result = service.run(
            user=owner,
            project=project,
            user_request=user_request,
            candidates=candidates,
        )
    except Exception:  # pragma: no cover - defensive
        response["warnings"].append("rerank_provider_error")
        response["capabilities"]["small_model_rerank"] = "error"
        return False
    if result is None:
        response["capabilities"]["small_model_rerank"] = "disabled"
        return False
    if result.get("_error") == "QUOTA_EXCEEDED":
        response["warnings"].append("rerank_quota_exhausted")
        response["capabilities"]["small_model_rerank"] = "quota_exhausted"
        return False
    ranked = result.get("ranked") or []
    if not ranked:
        response["capabilities"]["small_model_rerank"] = "error"
        return False
    by_id = {f"c{idx}": t for idx, t in enumerate(read_targets)}
    new_targets: list[dict] = []
    seen: set[str] = set()
    for entry in ranked:
        cid = entry.get("candidate_id")
        if cid in by_id and cid not in seen:
            target = dict(by_id[cid])
            target["confidence"] = entry.get("confidence") or target.get("confidence")
            target["rerank_reason"] = entry.get("reason") or ""
            new_targets.append(target)
            seen.add(cid)
    for cid, target in by_id.items():
        if cid not in seen:
            new_targets.append(target)
    response["read_targets"] = new_targets
    # Re-derive likely_edit_targets order to follow the new ranking.
    name_order = {t["filename"]: i for i, t in enumerate(new_targets)}
    response["likely_edit_targets"] = sorted(
        response.get("likely_edit_targets") or [],
        key=lambda e: name_order.get(e.get("filename"), 1_000),
    )
    response["capabilities"]["small_model_rerank"] = "used"
    scope = result.get("scope_confidence")
    if scope in {"low", "medium", "high"}:
        response["scope_confidence"] = scope
    return True


def _maybe_small_model_repair(
    *,
    project: Project,
    previous_error: Optional[dict],
    attempted_patch_ops: Optional[list[dict]],
    deterministic_guidance: dict,
    response: dict[str, Any],
) -> Optional[dict]:
    try:
        from small_model.services.nav_repair_guidance import NavRepairGuidanceService
    except Exception:  # pragma: no cover
        return None
    owner = getattr(project, "owner", None)
    if owner is None:
        return None
    service = NavRepairGuidanceService()
    try:
        enabled, _, _ = service.is_enabled(owner, project)
    except Exception:
        enabled = False
    if not enabled:
        return None
    try:
        result = service.run(
            user=owner,
            project=project,
            previous_error=previous_error or {},
            attempted_patch_ops=attempted_patch_ops,
            patch_op_reminder=response.get("patch_op_schema_reminder"),
            candidate_targets=response.get("read_targets") or [],
        )
    except Exception:  # pragma: no cover - defensive
        response["warnings"].append("repair_provider_error")
        response["capabilities"]["small_model_repair"] = "error"
        return None
    if result is None:
        response["capabilities"]["small_model_repair"] = "disabled"
        return None
    if result.get("_error") == "QUOTA_EXCEEDED":
        response["warnings"].append("repair_quota_exhausted")
        response["capabilities"]["small_model_repair"] = "quota_exhausted"
        return None
    response["capabilities"]["small_model_repair"] = "used"
    return result


def _merge_repair_guidance(deterministic: dict, sm: dict) -> dict:
    """Deterministic guidance WINS for ``error_kind`` and the ``fix_hint``
    op fields; small-model adds the human-readable diagnosis and extra
    read targets.
    """
    if not deterministic:
        return sm
    merged = dict(deterministic)
    if sm.get("diagnosis"):
        merged["diagnosis"] = sm["diagnosis"]
    det_fix = dict(deterministic.get("fix_hint") or {})
    sm_fix = dict(sm.get("fix_hint") or {})
    if not det_fix.get("rewrite_op") and sm_fix.get("rewrite_op"):
        det_fix["rewrite_op"] = sm_fix["rewrite_op"]
    if not det_fix.get("add_op") and sm_fix.get("add_op"):
        det_fix["add_op"] = sm_fix["add_op"]
    existing_targets = set(det_fix.get("additional_read_targets") or [])
    for t in sm_fix.get("additional_read_targets") or []:
        if t not in existing_targets:
            det_fix.setdefault("additional_read_targets", []).append(t)
            existing_targets.add(t)
    if sm_fix.get("notes"):
        det_fix["notes"] = sm_fix["notes"]
    merged["fix_hint"] = det_fix
    return merged
