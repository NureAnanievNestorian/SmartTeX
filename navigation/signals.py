"""Signal-driven freshness for the navigation index.

These handlers are deliberately defensive: any failure is logged and
swallowed so that user-facing writes (accepting a proposal, snapshotting
a version, etc.) never block on navigation bookkeeping.
"""
from __future__ import annotations

import logging
from typing import Iterable

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)

def _refresh_async_or_sync(project_id: int, files: Iterable[str]) -> None:
    """Best-effort partial refresh.

    Synchronous today (no Celery in the stack). If files are empty we
    skip — we don't want to revalidate the whole project on every signal.
    """
    files = [f for f in (files or []) if f]
    if not files:
        return
    try:
        from projects.models import Project
        from .services.refresh import refresh_navigation_index

        project = Project.objects.filter(pk=project_id).first()
        if project is None:
            return
        summary = refresh_navigation_index(project, files=files)
        logger.info(
            "nav.partial_refresh project=%s files=%d updated=%d missing=%d status=%s",
            project_id,
            len(files),
            summary.files_updated,
            summary.files_marked_missing,
            summary.status,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("nav partial refresh failed for project %s: %s", project_id, exc)


def _mark_stale(project_id: int, files: Iterable[str]) -> None:
    try:
        from projects.models import Project
        from .services.refresh import mark_files_stale

        project = Project.objects.filter(pk=project_id).first()
        if project is None:
            return
        mark_files_stale(project, files)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("nav mark_files_stale failed for project %s: %s", project_id, exc)


def _mark_whole(project_id: int, reason: str) -> None:
    try:
        from projects.models import Project
        from .services.refresh import mark_whole_index_stale

        project = Project.objects.filter(pk=project_id).first()
        if project is None:
            return
        mark_whole_index_stale(project, reason=reason)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("nav mark_whole_index_stale failed for project %s: %s", project_id, exc)


def _register() -> None:
    """Connect signals lazily so optional apps don't crash imports."""
    try:
        from projects.models import Project, ProjectVersion
    except Exception:  # pragma: no cover
        return

    try:
        from longdoc.models import ChangeProposal
    except Exception:  # pragma: no cover
        ChangeProposal = None  # type: ignore

    @receiver(post_save, sender=ProjectVersion, dispatch_uid="nav_index_on_projectversion")
    def _on_version(sender, instance: "ProjectVersion", created: bool, **kwargs) -> None:
        if not created:
            return
        files: list[str] = []
        tgt = getattr(instance, "target_file", "") or getattr(instance, "target", "")
        if tgt:
            files.append(tgt)
        payload = getattr(instance, "event_payload", None) or {}
        for key in ("files", "changed_files", "affected_files"):
            extra = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(extra, list):
                for f in extra:
                    if isinstance(f, str) and f:
                        files.append(f)
        files = sorted({f for f in files if f})
        # Always mark stale first (cheap), then attempt refresh.
        _mark_stale(instance.project_id, files)
        _refresh_async_or_sync(instance.project_id, files)

    @receiver(post_save, sender=Project, dispatch_uid="nav_index_on_project_meta")
    def _on_project(sender, instance: "Project", created: bool, **kwargs) -> None:
        if created:
            # Create a pending index stub so the first prepare_document_work
            # knows an index is expected and will build synchronously.
            try:
                from .models import IndexStatus, ProjectNavigationIndex
                ProjectNavigationIndex.objects.get_or_create(
                    project=instance,
                    defaults={"status": IndexStatus.PENDING},
                )
            except Exception as exc:
                logger.warning("nav pending index creation failed: %s", exc)
            return
        # Markup-type / main-file changes invalidate the whole index
        # (entrypoint/structural facts change). Detection is best-effort:
        # we just mark whole stale; the next preparation call rebuilds.
        try:
            from .services.freshness import evaluate_index

            freshness = evaluate_index(instance)
            if freshness.status in {"whole_invalid"}:
                _mark_whole(instance.id, "project_metadata_changed")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("nav project-meta evaluation failed: %s", exc)

    if ChangeProposal is not None:
        @receiver(
            post_save,
            sender=ChangeProposal,
            dispatch_uid="nav_index_on_changeproposal",
        )
        def _on_proposal(sender, instance: "ChangeProposal", created: bool, **kwargs) -> None:
            if getattr(instance, "status", "") != ChangeProposal.Status.ACCEPTED:
                return
            files = []
            raw = getattr(instance, "changed_files", None) or []
            if isinstance(raw, list):
                for entry in raw:
                    if isinstance(entry, str) and entry:
                        files.append(entry)
                    elif isinstance(entry, dict):
                        f = entry.get("filename") or entry.get("file") or entry.get("path")
                        if isinstance(f, str) and f:
                            files.append(f)
            files = sorted(set(files))
            _mark_stale(instance.project_id, files)
            _refresh_async_or_sync(instance.project_id, files)

    @receiver(post_delete, sender=Project, dispatch_uid="nav_index_on_project_delete")
    def _on_project_delete(sender, instance: "Project", **kwargs) -> None:
        # FK cascade already removes ProjectNavigationIndex; nothing to do.
        # The handler exists so that future bookkeeping (eg. cache purge)
        # has a hook.
        return

    # Mark the project index stale when nav-specific settings change.
    # Only fires for ProjectSmallModelSettings saves; other settings models
    # (quota, UserSmallModelAccess, etc.) are updated frequently and are
    # not connected here.
    try:
        from small_model.models import ProjectSmallModelSettings

        _NAV_FIELDS = frozenset({"nav_index_enrich_enabled", "nav_rerank_enabled", "nav_repair_enabled"})

        @receiver(
            post_save,
            sender=ProjectSmallModelSettings,
            dispatch_uid="nav_index_on_smallmodel_settings",
        )
        def _on_smallmodel_settings(
            sender, instance: "ProjectSmallModelSettings", created: bool, update_fields=None, **kwargs
        ) -> None:
            # Skip if update_fields is known and doesn't include nav fields.
            if update_fields is not None and not (_NAV_FIELDS & set(update_fields)):
                return
            try:
                from .services.refresh import mark_whole_index_stale
                mark_whole_index_stale(instance.project, reason="nav_settings_changed")
            except Exception as exc:
                logger.warning("nav settings-change stale marking failed: %s", exc)

    except ImportError:
        pass
