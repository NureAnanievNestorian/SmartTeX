"""Read-only Smart Search for project documents.

Combines index-based scoring (triggers, summaries, titles, reachability,
file role, region kind, state) with optional small-model reranking.

Never edits files, never creates proposals, never reads outside the project.
All snippets are bounded to _MAX_SNIPPET_LINES lines / _MAX_SNIPPET_CHARS chars.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

from projects.models import Project
from projects.services import project_dir

from ..models import (
    FileCard,
    FileRole,
    ProjectNavigationIndex,
    Reachability,
    RegionCard,
    RegionKind,
    StateKind,
)
from .freshness import evaluate_index
from .index_builder import build_navigation_index

logger = logging.getLogger(__name__)

_MAX_RESULTS_CAP = 50
_MAX_RESULTS_DEFAULT = 20
_MAX_SNIPPET_LINES = 10
_MAX_SNIPPET_CHARS = 500
_MAX_CANDIDATES_FOR_RERANK = 20

_VALID_SCOPES = frozenset({"reachable_document", "current_file", "all_project_files"})
_VALID_MATCH_KINDS = frozenset({
    "exact_match", "semantic_match", "related_context", "possible_conflict",
    "placeholder_or_demo", "old_topic_residue", "citation_or_source",
    "diagram_reference", "definition",
})
_VALID_CONFIDENCE = frozenset({"low", "medium", "high"})

_OLD_TOPIC_PATH_RE = re.compile(
    r"(old[-_]|demo[-_]|draft[-_]|[-_]old\.[a-z]+|[-_]demo\.[a-z]+|"
    r"[-_]draft\.[a-z]+|/extra/|/archive/|BENCHMARK)",
    re.IGNORECASE,
)

_KEYWORD_RE = re.compile(r"[A-Za-zА-ЯҐЄІЇа-яґєії0-9]{3,}")


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------

def _extract_keywords(query: str) -> list[str]:
    """Return unique lowercased keywords, longest first."""
    words = _KEYWORD_RE.findall(query or "")
    seen: set[str] = set()
    result: list[str] = []
    for w in words:
        wl = w.lower()
        if wl not in seen:
            seen.add(wl)
            result.append(wl)
    result.sort(key=len, reverse=True)
    return result



# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def _read_file_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _keyword_match_lines(text: str, keywords: list[str]) -> list[int]:
    """Return 1-indexed line numbers containing any keyword (case-insensitive)."""
    matched: list[int] = []
    for i, line in enumerate(text.splitlines(), start=1):
        low = line.lower()
        if any(k in low for k in keywords):
            matched.append(i)
    return matched


# ---------------------------------------------------------------------------
# Snippet extraction (always bounded)
# ---------------------------------------------------------------------------

def _extract_snippet(
    lines: list[str],
    *,
    line_start: int,
    line_end: int,
    anchor_lines: Optional[list[int]] = None,
) -> str:
    if not lines:
        return ""
    total = len(lines)
    line_start = max(1, min(line_start, total))
    line_end = max(line_start, min(line_end, total))

    if anchor_lines:
        anchor = anchor_lines[0]
        start = max(line_start, anchor - 2)
        end = min(line_end, anchor + _MAX_SNIPPET_LINES - 3)
    else:
        start = line_start
        end = min(line_end, line_start + _MAX_SNIPPET_LINES - 1)

    start = max(1, start)
    end = min(end, start + _MAX_SNIPPET_LINES - 1)
    end = min(end, total)

    text = "\n".join(lines[start - 1: end])
    if len(text) > _MAX_SNIPPET_CHARS:
        text = text[:_MAX_SNIPPET_CHARS] + "…"
    return text


# ---------------------------------------------------------------------------
# Match kind determination
# ---------------------------------------------------------------------------

def _determine_match_kind(
    file_card: FileCard,
    region: Optional[RegionCard],
    *,
    has_exact_text: bool,
    trigger_score: float,
) -> str:
    if region:
        if region.region_kind == RegionKind.FIGURE_BLOCK:
            return "diagram_reference"
        if region.region_kind == RegionKind.BIBLIOGRAPHY_BLOCK:
            return "citation_or_source"
        if region.region_kind == RegionKind.METADATA_BLOCK:
            return "definition"

    if file_card.role in {FileRole.BIB, FileRole.CSL}:
        return "citation_or_source"

    state = (region.state if region else file_card.state)
    if state in {StateKind.PLACEHOLDER, StateKind.DEMO}:
        return "placeholder_or_demo"

    if _OLD_TOPIC_PATH_RE.search(file_card.filename):
        return "old_topic_residue"

    if has_exact_text:
        return "exact_match"

    if trigger_score > 0:
        return "semantic_match"

    return "related_context"


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score_candidate(
    file_card: FileCard,
    region: Optional[RegionCard],
    keywords: list[str],
    *,
    scope: str,
    include_orphans: bool,
    include_extra: bool,
    include_config: bool,
) -> tuple[float, dict[str, float]]:
    """Score a (FileCard, RegionCard?) candidate. Returns (total, components)."""
    components: dict[str, float] = {}

    if file_card.reachability in {Reachability.MISSING, Reachability.EXCLUDED}:
        return -999.0, {}
    if file_card.role == FileRole.CONFIG and not include_config:
        return -999.0, {}

    score = 0.0
    kw_set = set(keywords)

    # Orphan penalty.
    if file_card.reachability == Reachability.ORPHAN and not include_orphans:
        if scope == "reachable_document":
            return -999.0, {}
        components["orphan_penalty"] = -1.5
        score -= 1.5

    # Extra/old content penalty.
    if _OLD_TOPIC_PATH_RE.search(file_card.filename) and not include_extra:
        components["extra_penalty"] = -1.0
        score -= 1.0

    # File-level trigger match.
    trigger_score = 0.0
    for trig in file_card.edit_triggers or []:
        phrase = str((trig or {}).get("phrase", "")).lower()
        if not phrase:
            continue
        if phrase in kw_set:
            trigger_score += 2.0
        elif any(k in phrase or phrase in k for k in keywords):
            trigger_score += 0.8
    components["trigger"] = trigger_score
    score += trigger_score

    # Region-level trigger match.
    if region:
        region_trigger = 0.0
        for trig in region.edit_triggers or []:
            phrase = str((trig or {}).get("phrase", "")).lower()
            if not phrase:
                continue
            if phrase in kw_set:
                region_trigger += 2.5
            elif any(k in phrase or phrase in k for k in keywords):
                region_trigger += 0.8
        components["region_trigger"] = region_trigger
        score += region_trigger

    # Title match.
    title_str = (region.title if region else Path(file_card.filename).stem).lower()
    title_score = 0.0
    if title_str:
        if any(k in title_str or title_str in k for k in keywords):
            title_score += 3.0
        for token in re.split(r"[\-_./\s]+", title_str):
            if token and token in kw_set:
                title_score += 1.0
    components["title"] = title_score
    score += title_score

    # Summary match.
    summary = ((region.summary if region else None) or file_card.summary or "").lower()
    summary_score = 0.0
    if summary and keywords:
        if any(k in summary for k in keywords):
            summary_score += 1.5
        elif any(any(k in w or w in k for k in keywords) for w in summary.split()):
            summary_score += 0.5
    components["summary"] = summary_score
    score += summary_score

    # Role prior.
    if file_card.role == FileRole.ENTRYPOINT:
        score += 0.4
        components["role"] = 0.4
    elif file_card.role in {FileRole.CONTENT_SECTION, FileRole.METADATA}:
        score += 0.3
        components["role"] = 0.3

    # Reachability bonus.
    if file_card.reachability == Reachability.REACHABLE:
        score += 0.4
        components["reachability"] = 0.4

    # State boost (demo/placeholder regions are interesting for certain queries).
    state = (region.state if region else file_card.state)
    if state in {StateKind.PLACEHOLDER, StateKind.DEMO}:
        score += 0.3
        components["state_boost"] = 0.3

    # Staleness penalty.
    if file_card.is_stale:
        score -= 0.3
    if region and region.is_stale:
        score -= 0.2

    return score, components


# ---------------------------------------------------------------------------
# Candidate building
# ---------------------------------------------------------------------------

def _build_candidates(
    index: ProjectNavigationIndex,
    keywords: list[str],
    *,
    scope: str,
    selected_file: Optional[str],
    include_orphans: bool,
    include_extra: bool,
    include_config: bool,
) -> list[tuple[float, dict, FileCard, Optional[RegionCard]]]:
    results: list[tuple[float, dict, FileCard, Optional[RegionCard]]] = []

    qs = index.file_cards.exclude(
        reachability__in=[Reachability.MISSING, Reachability.EXCLUDED]
    ).prefetch_related("region_cards")

    if scope == "current_file" and selected_file:
        qs = qs.filter(filename=selected_file)
    elif scope == "reachable_document" and not include_orphans:
        qs = qs.filter(
            reachability__in=[Reachability.REACHABLE, Reachability.DYNAMIC_UNRESOLVED]
        )

    for fc in qs:
        best_region: Optional[RegionCard] = None
        best_region_score = -999.0
        best_region_comps: dict = {}

        for region in fc.region_cards.all():
            s, comps = _score_candidate(
                fc, region, keywords,
                scope=scope, include_orphans=include_orphans,
                include_extra=include_extra, include_config=include_config,
            )
            if s > best_region_score:
                best_region_score = s
                best_region = region
                best_region_comps = comps

        file_score, file_comps = _score_candidate(
            fc, None, keywords,
            scope=scope, include_orphans=include_orphans,
            include_extra=include_extra, include_config=include_config,
        )

        if best_region is not None and best_region_score >= file_score and best_region_score > 0:
            results.append((best_region_score, best_region_comps, fc, best_region))
        elif file_score > 0:
            results.append((file_score, file_comps, fc, None))

    results.sort(key=lambda x: x[0], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Result building
# ---------------------------------------------------------------------------

def _build_result(
    file_card: FileCard,
    region: Optional[RegionCard],
    score: float,
    score_components: dict,
    keywords: list[str],
    file_text: Optional[str],
) -> dict[str, Any]:
    all_lines = file_text.splitlines() if file_text else []
    total_lines = len(all_lines) or 1

    line_start = max(1, (region.line_start if region else 1))
    line_end = min(total_lines, (region.line_end if region else total_lines))

    # Exact-text scan within region bounds.
    has_exact = False
    anchor_lines: list[int] = []
    if all_lines and keywords:
        region_text = "\n".join(all_lines[line_start - 1: line_end]).lower()
        if any(k in region_text for k in keywords):
            has_exact = True
            for i, line in enumerate(all_lines[line_start - 1: line_end], start=line_start):
                if any(k in line.lower() for k in keywords):
                    anchor_lines.append(i)

    trigger_score = score_components.get("trigger", 0) + score_components.get("region_trigger", 0)
    match_kind = _determine_match_kind(
        file_card, region,
        has_exact_text=has_exact,
        trigger_score=trigger_score,
    )

    if score >= 5.0:
        confidence = "high"
    elif score >= 2.5:
        confidence = "medium"
    else:
        confidence = "low"

    reason_parts: list[str] = []
    if trigger_score > 0:
        reason_parts.append("trigger match")
    if score_components.get("title", 0) > 0:
        reason_parts.append("title match")
    if score_components.get("summary", 0) > 0:
        reason_parts.append("summary match")
    if has_exact:
        reason_parts.append("exact text found in region")
    if not reason_parts:
        reason_parts.append("structural relevance")
    reason = "; ".join(reason_parts)

    snippet = _extract_snippet(
        all_lines,
        line_start=line_start,
        line_end=line_end,
        anchor_lines=anchor_lines or None,
    )

    return {
        "filename": file_card.filename,
        "line_start": line_start,
        "line_end": line_end,
        "region_title": (region.title if region else ""),
        "region_kind": (region.region_kind if region else ""),
        "match_kind": match_kind,
        "confidence": confidence,
        "reason": reason,
        "snippet": snippet,
        "file_role": file_card.role,
        "file_state": (region.state if region else file_card.state),
        "reachability": file_card.reachability,
        "_score": score,
    }


# ---------------------------------------------------------------------------
# Fallback (no index)
# ---------------------------------------------------------------------------

_FALLBACK_EXTENSIONS = {".tex", ".typ", ".md", ".txt", ".bib", ".puml"}


def _fallback_filesystem_search(
    project: Project,
    keywords: list[str],
    *,
    max_results: int,
) -> list[dict[str, Any]]:
    root = project_dir(project)
    if not root.exists():
        return []

    results: list[dict] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in _FALLBACK_EXTENSIONS:
            continue
        rel = path.relative_to(root).as_posix()
        if ".git/" in rel or ".smarttex/" in rel:
            continue

        text = _read_file_text(path)
        if not text:
            continue
        all_lines = text.splitlines()
        matched = _keyword_match_lines(text, keywords)
        if not matched:
            continue

        snippet = _extract_snippet(
            all_lines,
            line_start=matched[0],
            line_end=min(matched[0] + _MAX_SNIPPET_LINES, len(all_lines)),
            anchor_lines=matched[:3],
        )
        results.append({
            "filename": rel,
            "line_start": matched[0],
            "line_end": min(matched[0] + _MAX_SNIPPET_LINES, len(all_lines)),
            "region_title": "",
            "region_kind": "",
            "match_kind": "exact_match",
            "confidence": "low",
            "reason": "filesystem text search (no index available)",
            "snippet": snippet,
            "file_role": "unknown",
            "file_state": "unknown",
            "reachability": "unknown",
        })
        if len(results) >= max_results:
            break

    return results


# ---------------------------------------------------------------------------
# Small model integration
# ---------------------------------------------------------------------------

def _maybe_rerank(
    project: Project,
    query: str,
    results: list[dict],
    capabilities: dict,
) -> bool:
    try:
        from small_model.services.search_reranker import SearchRerankerService
    except ImportError:
        capabilities["small_model_search"] = "unavailable"
        return False

    owner = getattr(project, "owner", None)
    if owner is None:
        capabilities["small_model_search"] = "unavailable"
        return False

    service = SearchRerankerService()
    try:
        enabled, _, _ = service.is_enabled(owner, project)
    except Exception:
        enabled = False

    if not enabled:
        capabilities["small_model_search"] = "disabled"
        return False

    candidates = [
        {
            "candidate_id": f"r{i}",
            "filename": r["filename"],
            "region_title": r.get("region_title") or "",
            "match_kind": r.get("match_kind") or "",
            "confidence": r.get("confidence") or "low",
            "reason": r.get("reason") or "",
            "snippet": (r.get("snippet") or "")[:200],
        }
        for i, r in enumerate(results[:_MAX_CANDIDATES_FOR_RERANK])
    ]

    try:
        result = service.run(
            user=owner, project=project, query=query, candidates=candidates,
        )
    except Exception as exc:
        logger.warning("search reranker failed: %s", exc)
        capabilities["small_model_search"] = "error"
        return False

    if result is None:
        capabilities["small_model_search"] = "disabled"
        return False
    if result.get("_error") == "QUOTA_EXCEEDED":
        capabilities["small_model_search"] = "quota_exhausted"
        return False

    ranked = result.get("ranked") or []
    if not ranked:
        capabilities["small_model_search"] = "error"
        return False

    by_id = {f"r{i}": r for i, r in enumerate(results[:_MAX_CANDIDATES_FOR_RERANK])}
    new_results: list[dict] = []
    seen: set[str] = set()

    for entry in ranked:
        cid = str(entry.get("candidate_id") or "")
        if cid not in by_id or cid in seen:
            continue
        r = dict(by_id[cid])
        if entry.get("match_kind") in _VALID_MATCH_KINDS:
            r["match_kind"] = entry["match_kind"]
        if entry.get("confidence") in _VALID_CONFIDENCE:
            r["confidence"] = entry["confidence"]
        if entry.get("reason"):
            r["reason"] = str(entry["reason"])[:300]
        new_results.append(r)
        seen.add(cid)

    for cid, r in by_id.items():
        if cid not in seen:
            new_results.append(r)
    new_results.extend(results[_MAX_CANDIDATES_FOR_RERANK:])

    results[:] = new_results
    capabilities["small_model_search"] = "used"
    return True


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def smart_search(
    project: Project,
    *,
    query: str,
    scope: str = "reachable_document",
    selected_file: Optional[str] = None,
    include_orphans: bool = False,
    include_extra: bool = False,
    include_config: bool = False,
    use_small_model: bool = True,
    max_results: int = _MAX_RESULTS_DEFAULT,
) -> dict[str, Any]:
    """Run Smart Search on a project. Read-only; never edits files."""
    import time as _time
    _t0 = _time.monotonic()

    query = (query or "").strip()
    scope = scope if scope in _VALID_SCOPES else "reachable_document"
    max_results = max(1, min(int(max_results), _MAX_RESULTS_CAP))

    warnings: list[str] = []
    capabilities: dict[str, str] = {
        "navigation_index": "unavailable",
        "small_model_search": "unavailable",
    }

    keywords = _extract_keywords(query)
    if not keywords:
        return {
            "query": query,
            "mode": "fallback",
            "scope": scope,
            "results": [],
            "warnings": ["empty_query"],
            "capabilities": capabilities,
            "latency_ms": 0.0,
        }

    # Index lifecycle: build if absent.
    freshness = evaluate_index(project)
    index: Optional[ProjectNavigationIndex] = None

    if freshness.status == "absent":
        try:
            _sm = getattr(
                getattr(project, "small_model_settings", None),
                "nav_index_enrich_enabled", False,
            )
            build_navigation_index(project, use_small_model=bool(_sm))
            freshness = evaluate_index(project)
        except Exception as exc:
            logger.warning("smart_search index build failed: %s", exc)
            warnings.append("index_build_failed")

    try:
        index = ProjectNavigationIndex.objects.get(project=project)
    except ProjectNavigationIndex.DoesNotExist:
        index = None

    _INDEX_STATE_MAP = {
        "current": "ready",
        "partial_stale": "stale",
        "whole_invalid": "stale",
        "absent": "missing",
        "failed": "failed",
        "building": "unavailable",
    }
    if index:
        capabilities["navigation_index"] = _INDEX_STATE_MAP.get(freshness.status, "ready")
        if freshness.status in {"partial_stale", "whole_invalid"}:
            warnings.append("index_stale")
    else:
        capabilities["navigation_index"] = "missing"

    mode = "fallback"
    results: list[dict[str, Any]] = []

    index_usable = index and capabilities["navigation_index"] not in {"unavailable", "failed"}

    if index_usable:
        mode = "indexed_keyword"
        root = project_dir(project)
        file_text_cache: dict[str, Optional[str]] = {}

        candidates = _build_candidates(
            index, keywords,
            scope=scope,
            selected_file=selected_file,
            include_orphans=include_orphans,
            include_extra=include_extra,
            include_config=include_config,
        )

        if not candidates:
            warnings.append("no_index_match")

        for score, comps, fc, region in candidates[: max_results * 2]:
            if len(results) >= max_results:
                break
            fn = fc.filename
            if fn not in file_text_cache:
                file_text_cache[fn] = _read_file_text(root / fn)
            results.append(_build_result(fc, region, score, comps, keywords, file_text_cache[fn]))

        if use_small_model and len(results) >= 2:
            try:
                if _maybe_rerank(project, query, results, capabilities):
                    mode = "smart_reranked"
            except Exception as exc:
                logger.warning("smart_search rerank error: %s", exc)
                warnings.append("rerank_error")
    else:
        warnings.append("index_unavailable_using_fallback")
        results = _fallback_filesystem_search(project, keywords, max_results=max_results)

    for r in results:
        r.pop("_score", None)

    latency_ms = round((_time.monotonic() - _t0) * 1000.0, 1)
    logger.info(
        "nav.smart_search project=%s query=%r mode=%s results=%d latency_ms=%.1f warnings=%s",
        project.id, query[:40], mode, len(results), latency_ms, warnings,
    )

    return {
        "query": query,
        "mode": mode,
        "scope": scope,
        "results": results[:max_results],
        "warnings": warnings,
        "capabilities": capabilities,
        "latency_ms": latency_ms,
    }
