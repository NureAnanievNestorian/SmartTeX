"""Deterministic navigation index builder, with optional small-model
enrichment overlay.

Structural facts (role for bib/csl/style/class/entrypoint, includes
graph, line ranges, reachability) are ALWAYS deterministic. The small
model only fills in summaries, refines fuzzy states, and adds extra
edit triggers — and only when the project owner has enabled the
``nav_index_enrich`` feature.
"""
from __future__ import annotations

import logging
import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from django.db import transaction
from django.utils import timezone

from projects.models import Project, ProjectVersion
from projects.services import main_source_filename, project_dir

from longdoc.document_graph import inspect_document_graph

from ..models import (
    Confidence,
    FileCard,
    FileRole,
    IndexStatus,
    NAV_SCHEMA_VERSION,
    ProjectNavigationIndex,
    Reachability,
    RegionCard,
    Source,
    StateKind,
)
from .discovery import (
    DiscoveredFile,
    DiscoveryResult,
    classify_file_role,
    discover_project_files,
)
from .state_heuristics import classify_state
from .structure import (
    RegionInfo,
    deterministic_file_triggers,
    deterministic_region_triggers,
    extract_regions,
)

logger = logging.getLogger(__name__)


def _canon_graph_path(value: str | None) -> str:
    if not value:
        return ""
    raw = str(value).replace("\\", "/")
    normalized = posixpath.normpath(raw)
    if normalized == ".":
        return ""
    if normalized.startswith("./"):
        return normalized[2:]
    return normalized


def _graph_get(graph, key: str, default=None):
    if isinstance(graph, dict):
        return graph.get(key, default)
    return getattr(graph, key, default)


@dataclass
class BuildSummary:
    project_id: int
    status: str
    files_discovered: int = 0
    file_cards_created: int = 0
    file_cards_updated: int = 0
    file_cards_marked_missing: int = 0
    region_cards_created: int = 0
    region_cards_updated: int = 0
    region_cards_deleted: int = 0
    skipped_files: int = 0
    skip_reasons: dict = field(default_factory=dict)
    build_error: str = ""
    enrichment_status: str = "disabled"
    file_cards_enriched: int = 0
    region_cards_enriched: int = 0
    enrichment_budget_exhausted: bool = False

    def as_text(self) -> str:
        lines = [
            f"Project {self.project_id}: status={self.status}",
            f"  files discovered: {self.files_discovered}",
            f"  file cards: created={self.file_cards_created} updated={self.file_cards_updated} "
            f"marked_missing={self.file_cards_marked_missing}",
            f"  region cards: created={self.region_cards_created} updated={self.region_cards_updated} "
            f"deleted={self.region_cards_deleted}",
            f"  skipped files: {self.skipped_files} reasons={self.skip_reasons or {}}",
        ]
        if self.build_error:
            lines.append(f"  build_error: {self.build_error}")
        return "\n".join(lines)


def _latest_version_number(project: Project) -> int:
    latest = (
        ProjectVersion.objects.filter(project=project)
        .order_by("-number")
        .values_list("number", flat=True)
        .first()
    )
    return int(latest or 0)


def build_navigation_index(
    project: Project,
    *,
    use_small_model: bool = False,
    force: bool = False,
    enrichment_budget: int = 40,
) -> BuildSummary:
    """Build (or refresh) the navigation index for ``project``.

    When ``use_small_model`` is True and the project owner has enabled
    ``nav_index_enrich``, the small model is invoked per-card up to
    ``enrichment_budget`` total calls (file + region). On quota or
    provider failure, the build keeps deterministic cards and continues.
    """
    summary = BuildSummary(project_id=project.id, status="building")
    _t0 = timezone.now()
    logger.info("nav.build start project=%s use_small_model=%s force=%s",
                project.id, use_small_model, force)
    index, _ = ProjectNavigationIndex.objects.get_or_create(project=project)

    index.status = IndexStatus.BUILDING
    index.markup_type_snapshot = project.markup_type
    index.main_file_snapshot = _canon_graph_path(main_source_filename(project))
    index.entrypoint_file = _canon_graph_path(main_source_filename(project))
    index.schema_version = NAV_SCHEMA_VERSION
    index.build_error = ""
    index.save(
        update_fields=[
            "status",
            "markup_type_snapshot",
            "main_file_snapshot",
            "entrypoint_file",
            "schema_version",
            "build_error",
            "updated_at",
        ]
    )

    try:
        discovery = discover_project_files(project)
        summary.files_discovered = len(discovery.files)
        summary.skipped_files = len(discovery.skipped)
        summary.skip_reasons = dict(discovery.skip_reasons)

        graph = None
        try:
            graph = inspect_document_graph(project)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("document_graph inspection failed for project %s: %s", project.id, exc)

        reachable_set = {_canon_graph_path(p) for p in (_graph_get(graph, "reachable_files", []) or [])} if graph else set()
        orphan_set = {_canon_graph_path(p) for p in (_graph_get(graph, "orphan_source_files", []) or [])} if graph else set()
        dynamic_unresolved = set()
        if graph:
            for item in (_graph_get(graph, "unresolved_dynamic_imports", []) or []):
                if isinstance(item, dict):
                    dynamic_unresolved.add(_canon_graph_path(item.get("file")))
                else:
                    dynamic_unresolved.add(_canon_graph_path(item))
        # Edges: rebuild forward edges by re-parsing reachable files quickly via graph isn't exposed,
        # so we leave forward/reverse edges empty until refresh.py wires them. Keeping deterministic.
        includes_map = _build_includes_map(project, discovery.files, graph)

        version_number = _latest_version_number(project)

        # Upsert file cards
        for df in discovery.files:
            try:
                created, updated, region_changes = _upsert_file_card(
                    index=index,
                    df=df,
                    project=project,
                    reachable_set=reachable_set,
                    orphan_set=orphan_set,
                    dynamic_unresolved=dynamic_unresolved,
                    includes_map=includes_map,
                    version_number=version_number,
                )
                if created:
                    summary.file_cards_created += 1
                elif updated:
                    summary.file_cards_updated += 1
                summary.region_cards_created += region_changes["created"]
                summary.region_cards_updated += region_changes["updated"]
                summary.region_cards_deleted += region_changes["deleted"]
            except Exception as exc:  # pragma: no cover - per-file isolation
                logger.exception("Failed to index file %s: %s", df.filename, exc)
                # Mark a degraded card so we don't drop the file silently.
                _mark_file_stale(index, df.filename)

        # Record excluded discoveries as cards too (so the preparation tool can
        # surface "do not edit binary asset X").
        for df in discovery.skipped:
            try:
                _upsert_excluded_card(index=index, df=df)
            except Exception:  # pragma: no cover
                logger.exception("Failed to record excluded file %s", df.filename)

        # Small-model enrichment overlay (post-deterministic, best-effort).
        if use_small_model:
            try:
                budget_state = _enrich_cards(
                    project=project,
                    index=index,
                    budget=int(enrichment_budget or 0),
                    summary=summary,
                )
                summary.enrichment_status = budget_state
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("Small-model enrichment failed for project %s", project.id)
                summary.enrichment_status = f"error:{type(exc).__name__}"
        else:
            summary.enrichment_status = "disabled"

        # Mark missing files: cards that existed but were not seen this build.
        seen_names = {_canon_graph_path(df.filename) for df in discovery.files} | {_canon_graph_path(df.filename) for df in discovery.skipped}
        missing = index.file_cards.exclude(filename__in=seen_names)
        for card in missing:
            card.reachability = Reachability.MISSING
            card.is_stale = True
            if not card.exclusion_reason:
                card.exclusion_reason = "file_deleted"
            card.save(
                update_fields=["reachability", "is_stale", "exclusion_reason", "updated_at"]
            )
            summary.file_cards_marked_missing += 1

        files_total = len(discovery.files) + len(discovery.skipped)
        index.coverage = {
            "files_total": files_total,
            "files_covered": len(discovery.files),
            "files_skipped": len(discovery.skipped),
            "skip_reasons": dict(discovery.skip_reasons),
            "enrichment_status": summary.enrichment_status,
            "file_cards_enriched": summary.file_cards_enriched,
            "region_cards_enriched": summary.region_cards_enriched,
        }
        index.last_built_at = timezone.now()
        index.last_built_version_number = version_number
        index.status = IndexStatus.READY
        summary.status = "ready"
        index.save(
            update_fields=[
                "coverage",
                "last_built_at",
                "last_built_version_number",
                "status",
                "updated_at",
            ]
        )
    except Exception as exc:
        logger.exception("Navigation index build failed for project %s", project.id)
        index.status = IndexStatus.FAILED
        index.build_error = str(exc)[:2000]
        index.save(update_fields=["status", "build_error", "updated_at"])
        summary.status = "failed"
        summary.build_error = str(exc)

    try:
        logger.info(
            "nav.build done project=%s status=%s files=%d enriched=f%s/r%s latency_ms=%.1f",
            project.id,
            summary.status,
            summary.files_discovered,
            summary.file_cards_enriched,
            summary.region_cards_enriched,
            (timezone.now() - _t0).total_seconds() * 1000.0,
        )
    except Exception:  # pragma: no cover
        pass
    return summary


def rebuild_navigation_index(
    project: Project, *, use_small_model: bool = False
) -> BuildSummary:
    """Clear all cards and rebuild from scratch."""
    clear_navigation_index(project)
    return build_navigation_index(project, use_small_model=use_small_model, force=True)


def clear_navigation_index(project: Project) -> None:
    ProjectNavigationIndex.objects.filter(project=project).delete()


# --- internals ---------------------------------------------------------------


def _build_includes_map(
    project: Project, files: list[DiscoveredFile], graph
) -> dict[str, dict[str, list[str]]]:
    """Best-effort forward/reverse include edges using the document graph result.

    The current graph object only exposes reachability, not per-file edges.
    We re-derive lightweight edges by re-parsing reachable .typ/.tex files.
    """
    from longdoc.document_graph import _typst_refs, _latex_refs, _resolve_ref
    from SmartTeX.markup import MarkupType

    forward: dict[str, list[str]] = {}
    reverse: dict[str, list[str]] = {}

    root = project_dir(project)
    markup = project.markup_type
    suffix_ok = {".typ", ".tex"}

    for df in files:
        if Path(df.filename).suffix.lower() not in suffix_ok:
            continue
        try:
            text = df.absolute_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        refs, _dyn = (
            _typst_refs(text) if markup == MarkupType.TYPST else _latex_refs(text)
        )
        out: list[str] = []
        for ref in refs:
            resolved = _canon_graph_path(_resolve_ref(df.filename, ref, markup))
            source_name = _canon_graph_path(df.filename)
            out.append(resolved)
            reverse.setdefault(resolved, []).append(source_name)
        if out:
            forward[_canon_graph_path(df.filename)] = sorted(set(out))

    for k in list(reverse.keys()):
        reverse[k] = sorted(set(reverse[k]))

    return {"forward": forward, "reverse": reverse}


def _upsert_file_card(
    *,
    index: ProjectNavigationIndex,
    df: DiscoveredFile,
    project: Project,
    reachable_set: set[str],
    orphan_set: set[str],
    dynamic_unresolved: set[str],
    includes_map: dict,
    version_number: int,
) -> tuple[bool, bool, dict]:
    filename = _canon_graph_path(df.filename)
    main_file = _canon_graph_path(main_source_filename(project))
    is_entrypoint = filename == main_file
    role, role_conf = classify_file_role(filename, is_entrypoint=is_entrypoint)

    if filename in dynamic_unresolved:
        reachability = Reachability.DYNAMIC_UNRESOLVED
    elif filename in reachable_set or is_entrypoint:
        reachability = Reachability.REACHABLE
    elif filename in orphan_set:
        reachability = Reachability.ORPHAN
    else:
        # Not a source file: orphan-by-default unless reachable as an asset.
        suffix = Path(df.filename).suffix.lower()
        if suffix in {".tex", ".typ"}:
            reachability = Reachability.ORPHAN
        else:
            reachability = Reachability.REACHABLE if filename in reachable_set else Reachability.ORPHAN

    try:
        content = df.absolute_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        content = ""

    state, state_conf = classify_state(content)

    region_infos = extract_regions(
        filename=filename,
        content=content,
        markup_type=project.markup_type,
        role=role,
    )
    region_titles = [r.title for r in region_infos if r.title]
    triggers = deterministic_file_triggers(
        filename=filename, role=role, region_titles=region_titles
    )

    forward = includes_map.get("forward", {}).get(filename, [])
    reverse = includes_map.get("reverse", {}).get(filename, [])

    summary_text = _build_file_summary(df=df, role=role, region_count=len(region_infos))

    with transaction.atomic():
        card, created = FileCard.objects.select_for_update().get_or_create(
            index=index,
            filename=filename,
            defaults={
                "role": role,
                "role_source": Source.DETERMINISTIC,
                "role_confidence": role_conf,
                "state": state,
                "state_source": Source.DETERMINISTIC,
                "state_confidence": state_conf,
                "reachability": reachability,
                "included_by_filenames": reverse,
                "includes_out_filenames": forward,
                "summary": summary_text,
                "summary_source": Source.DETERMINISTIC,
                "summary_confidence": Confidence.LOW,
                "edit_triggers": triggers,
                "triggers_source": Source.DETERMINISTIC,
                "line_count": df.line_count,
                "byte_size": df.byte_size,
                "content_hash": df.content_hash,
                "last_version_number": version_number,
                "last_indexed_at": timezone.now(),
                "is_stale": False,
                "exclusion_reason": "",
            },
        )

        updated = False
        if not created:
            unchanged = card.content_hash == df.content_hash and not card.is_stale
            preserve_sm_summary = unchanged and card.summary_source == Source.SMALL_MODEL and bool(card.summary)
            preserve_sm_triggers = unchanged and card.triggers_source == Source.SMALL_MODEL and bool(card.edit_triggers)
            old_summary = card.summary
            old_summary_source = card.summary_source
            old_summary_confidence = card.summary_confidence
            old_triggers = card.edit_triggers
            old_triggers_source = card.triggers_source
            # Always refresh deterministic metadata; cheaper to overwrite than diff.
            # Preserve small-model search metadata while content is unchanged;
            # re-enrich only changed cards so rebuilds don't waste tokens.
            card.role = role
            card.role_source = Source.DETERMINISTIC
            card.role_confidence = role_conf
            card.state = state
            card.state_source = Source.DETERMINISTIC
            card.state_confidence = state_conf
            card.reachability = reachability
            card.included_by_filenames = reverse
            card.includes_out_filenames = forward
            card.summary = summary_text
            card.summary_source = Source.DETERMINISTIC
            card.edit_triggers = triggers
            card.triggers_source = Source.DETERMINISTIC
            if preserve_sm_summary:
                card.summary = old_summary
                card.summary_source = old_summary_source
                card.summary_confidence = old_summary_confidence
            if preserve_sm_triggers:
                card.edit_triggers = old_triggers
                card.triggers_source = old_triggers_source
            card.line_count = df.line_count
            card.byte_size = df.byte_size
            card.content_hash = df.content_hash
            card.last_version_number = version_number
            card.last_indexed_at = timezone.now()
            card.is_stale = False
            card.exclusion_reason = ""
            card.save()
            updated = not unchanged

        region_changes = _sync_region_cards(card=card, region_infos=region_infos, content=content)

    return created, updated, region_changes


def _build_file_summary(*, df: DiscoveredFile, role: str, region_count: int) -> str:
    parts = [f"{role}"]
    if region_count:
        parts.append(f"{region_count} region(s)")
    parts.append(f"{df.line_count} lines")
    parts.append(f"{df.byte_size} bytes")
    text = "; ".join(parts)
    return text[:280]


def _sync_region_cards(
    *, card: FileCard, region_infos: list[RegionInfo], content: str
) -> dict[str, int]:
    """Upsert region cards for a file card and prune obsolete ones."""
    created = 0
    updated = 0
    deleted = 0

    existing = {rc.order: rc for rc in card.region_cards.all()}
    seen_orders: set[int] = set()

    for info in region_infos:
        seen_orders.add(info.order)
        triggers = deterministic_region_triggers(title=info.title)
        # State for regions: classify the region body slice.
        slice_text = "\n".join(
            content.splitlines()[max(0, info.line_start - 1): max(0, info.line_end)]
        )
        state, state_conf = classify_state(slice_text)
        defaults = {
            "region_kind": info.region_kind,
            "title": info.title[:512],
            "level": info.level,
            "line_start": info.line_start,
            "line_end": info.line_end,
            "state": state,
            "state_source": Source.DETERMINISTIC,
            "state_confidence": state_conf,
            "summary": (info.title or info.region_kind)[:280],
            "summary_source": Source.DETERMINISTIC,
            "summary_confidence": Confidence.LOW,
            "edit_triggers": triggers,
            "triggers_source": Source.DETERMINISTIC,
            "content_hash": info.content_hash,
            "last_indexed_at": timezone.now(),
            "is_stale": False,
        }

        existing_card = existing.get(info.order)
        if existing_card is None:
            RegionCard.objects.create(file_card=card, order=info.order, **defaults)
            created += 1
        else:
            unchanged = existing_card.content_hash == info.content_hash and not existing_card.is_stale
            if unchanged and existing_card.summary_source == Source.SMALL_MODEL and existing_card.summary:
                defaults["summary"] = existing_card.summary
                defaults["summary_source"] = existing_card.summary_source
                defaults["summary_confidence"] = existing_card.summary_confidence
            if unchanged and existing_card.triggers_source == Source.SMALL_MODEL and existing_card.edit_triggers:
                defaults["edit_triggers"] = existing_card.edit_triggers
                defaults["triggers_source"] = existing_card.triggers_source
            changed = False
            for k, v in defaults.items():
                if getattr(existing_card, k) != v:
                    setattr(existing_card, k, v)
                    changed = True
            if changed:
                existing_card.save()
                updated += 1

    # Prune regions whose order no longer exists.
    obsolete = [rc for order, rc in existing.items() if order not in seen_orders]
    for rc in obsolete:
        rc.delete()
        deleted += 1

    return {"created": created, "updated": updated, "deleted": deleted}


def _upsert_excluded_card(*, index: ProjectNavigationIndex, df: DiscoveredFile) -> None:
    filename = _canon_graph_path(df.filename)
    existing = FileCard.objects.filter(index=index, filename=filename).first()
    if existing is not None and existing.reachability != Reachability.EXCLUDED:
        # Defensive guard: do not overwrite a valid source/reachable card if
        # discovery ever reports the same path as skipped too.
        return
    FileCard.objects.update_or_create(
        index=index,
        filename=filename,
        defaults={
            "role": FileRole.AUXILIARY,
            "role_source": Source.DETERMINISTIC,
            "role_confidence": Confidence.LOW,
            "state": StateKind.UNKNOWN,
            "state_source": Source.DETERMINISTIC,
            "state_confidence": Confidence.LOW,
            "reachability": Reachability.EXCLUDED,
            "included_by_filenames": [],
            "includes_out_filenames": [],
            "summary": f"excluded: {df.exclusion_reason}"[:280],
            "summary_source": Source.DETERMINISTIC,
            "summary_confidence": Confidence.LOW,
            "edit_triggers": [],
            "triggers_source": Source.DETERMINISTIC,
            "line_count": df.line_count,
            "byte_size": df.byte_size,
            "content_hash": df.content_hash,
            "last_indexed_at": timezone.now(),
            "is_stale": False,
            "exclusion_reason": df.exclusion_reason or "excluded",
        },
    )


def _mark_file_stale(index: ProjectNavigationIndex, filename: str) -> None:
    FileCard.objects.filter(index=index, filename=_canon_graph_path(filename)).update(is_stale=True)


# --- small-model enrichment --------------------------------------------------


def _enrich_cards(
    *,
    project: Project,
    index: ProjectNavigationIndex,
    budget: int,
    summary: BuildSummary,
) -> str:
    """Per-card small-model enrichment. Returns a status string."""
    from small_model.services.nav_card_enricher import (
        NavFileCardEnrichService,
        NavRegionCardEnrichService,
    )

    user = getattr(project, "owner", None)
    if user is None:
        return "disabled"

    file_service = NavFileCardEnrichService()
    region_service = NavRegionCardEnrichService()

    enabled, _, _ = file_service.is_enabled(user, project)
    if not enabled:
        return "disabled"

    remaining = max(0, int(budget or 0))
    if remaining == 0:
        summary.enrichment_budget_exhausted = True
        return "budget_zero"

    root = project_dir(project)
    cards = list(
        index.file_cards.exclude(reachability=Reachability.EXCLUDED)
        .exclude(reachability=Reachability.MISSING)
    )

    seen_quota_error = False
    seen_provider_error = False

    for card in cards:
        if remaining <= 0:
            summary.enrichment_budget_exhausted = True
            break
        enrich_file = _card_needs_enrichment(card)
        abs_path = root / card.filename
        try:
            content = abs_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            content = ""
        headings = list(
            card.region_cards.exclude(title="").values_list("title", flat=True)[:24]
        )
        if enrich_file:
            try:
                file_result = file_service.run(
                    user=user,
                    project=project,
                    filename=card.filename,
                    deterministic_role=card.role,
                    deterministic_state=card.state,
                    line_count=card.line_count,
                    byte_size=card.byte_size,
                    heading_titles=headings,
                    representative_content=content,
                    includes_out=list(card.includes_out_filenames or []),
                    included_by=list(card.included_by_filenames or []),
                )
            except Exception:  # pragma: no cover - defensive
                file_result = None
                seen_provider_error = True
            remaining -= 1

            if file_result and file_result.get("_error") == "QUOTA_EXCEEDED":
                seen_quota_error = True
                break
            if file_result:
                if _apply_file_enrichment(card, file_result):
                    summary.file_cards_enriched += 1

        for region in list(card.region_cards.all()):
            if remaining <= 0:
                summary.enrichment_budget_exhausted = True
                break
            if not _region_needs_enrichment(region):
                continue
            # Skip tiny / trivial regions.
            span = max(1, int(region.line_end) - int(region.line_start) + 1)
            if span < 3 and not region.title:
                continue
            content_lines = content.splitlines()
            if region.region_kind == "metadata_block" and span < 3:
                # Extend slice to capture the assignment value, not just the #let line.
                context_start = max(0, region.line_start - 1)
                context_end = min(len(content_lines), region.line_end + 4)
                slice_lines = content_lines[context_start:context_end]
            else:
                slice_lines = content_lines[
                    max(0, region.line_start - 1): max(0, region.line_end)
                ]
            slice_text = "\n".join(slice_lines)
            try:
                region_result = region_service.run(
                    user=user,
                    project=project,
                    filename=card.filename,
                    region_title=region.title,
                    region_kind=region.region_kind,
                    line_start=region.line_start,
                    line_end=region.line_end,
                    deterministic_state=region.state,
                    representative_content=slice_text,
                )
            except Exception:  # pragma: no cover - defensive
                region_result = None
                seen_provider_error = True
            remaining -= 1
            if region_result and region_result.get("_error") == "QUOTA_EXCEEDED":
                seen_quota_error = True
                break
            if region_result and _apply_region_enrichment(region, region_result):
                summary.region_cards_enriched += 1
        if seen_quota_error:
            break

    if seen_quota_error:
        return "quota_exhausted"
    if summary.enrichment_budget_exhausted:
        return "budget_exhausted"
    if seen_provider_error and summary.file_cards_enriched == 0 and summary.region_cards_enriched == 0:
        return "error"
    return "active"


def _apply_file_enrichment(card: FileCard, result: dict) -> bool:
    """Apply small-model output to a FileCard. Returns True if anything changed.

    Deterministic structural facts (entrypoint/bib/csl/style/class roles,
    includes graph, line ranges, reachability) are NEVER overwritten.
    """
    changed = False
    summary = (result.get("summary") or "").strip()
    if summary:
        card.summary = summary[:280]
        card.summary_source = Source.SMALL_MODEL
        card.summary_confidence = _conf(result.get("summary_confidence"))
        changed = True
    state = result.get("state")
    if state and state != card.state and card.state_source == Source.DETERMINISTIC and card.state_confidence != Confidence.HIGH:
        # Only override fuzzy deterministic state.
        card.state = state
        card.state_source = Source.SMALL_MODEL
        card.state_confidence = _conf(result.get("state_confidence"))
        changed = True
    role_ref = result.get("role_refinement")
    if (
        role_ref
        and card.role in {FileRole.UNKNOWN, FileRole.AUXILIARY}
    ):
        try:
            card.role = role_ref
            card.role_source = Source.SMALL_MODEL
            card.role_confidence = _conf(result.get("role_confidence"))
            changed = True
        except Exception:  # pragma: no cover - defensive
            pass
    extra_triggers = result.get("edit_triggers") or []
    if extra_triggers:
        merged = list(card.edit_triggers or [])
        existing_phrases = {str(t.get("phrase") or "").lower() for t in merged if isinstance(t, dict)}
        added = False
        for trig in extra_triggers:
            phrase = (trig.get("phrase") or "").lower()
            if phrase and phrase not in existing_phrases:
                merged.append({"phrase": trig["phrase"], "weight": trig.get("weight", 1.0), "source": "small_model"})
                existing_phrases.add(phrase)
                added = True
        if added:
            card.edit_triggers = merged
            card.triggers_source = Source.SMALL_MODEL
            changed = True
    if changed:
        card.save()
    return changed


def _card_needs_enrichment(card: FileCard) -> bool:
    if card.is_stale:
        return True
    has_summary = card.summary_source == Source.SMALL_MODEL and bool(card.summary)
    has_triggers = card.triggers_source == Source.SMALL_MODEL and bool(card.edit_triggers)
    return not (has_summary and has_triggers)


def _region_needs_enrichment(region: RegionCard) -> bool:
    if region.is_stale:
        return True
    has_summary = region.summary_source == Source.SMALL_MODEL and bool(region.summary)
    has_triggers = region.triggers_source == Source.SMALL_MODEL and bool(region.edit_triggers)
    return not (has_summary and has_triggers)


def _apply_region_enrichment(region: RegionCard, result: dict) -> bool:
    changed = False
    summary = (result.get("summary") or "").strip()
    if summary:
        region.summary = summary[:280]
        region.summary_source = Source.SMALL_MODEL
        region.summary_confidence = _conf(result.get("summary_confidence"))
        changed = True
    state = result.get("state")
    if (
        state
        and state != region.state
        and region.state_source == Source.DETERMINISTIC
        and region.state_confidence != Confidence.HIGH
    ):
        region.state = state
        region.state_source = Source.SMALL_MODEL
        region.state_confidence = _conf(result.get("state_confidence"))
        changed = True
    extra_triggers = result.get("edit_triggers") or []
    if extra_triggers:
        merged = list(region.edit_triggers or [])
        existing = {str(t.get("phrase") or "").lower() for t in merged if isinstance(t, dict)}
        added = False
        for trig in extra_triggers:
            phrase = (trig.get("phrase") or "").lower()
            if phrase and phrase not in existing:
                merged.append({"phrase": trig["phrase"], "weight": trig.get("weight", 1.0), "source": "small_model"})
                existing.add(phrase)
                added = True
        if added:
            region.edit_triggers = merged
            region.triggers_source = Source.SMALL_MODEL
            changed = True
    if changed:
        region.save()
    return changed


def _conf(value: object) -> str:
    v = str(value or "").lower()
    if v == "high":
        return Confidence.HIGH
    if v == "medium":
        return Confidence.MEDIUM
    return Confidence.LOW
