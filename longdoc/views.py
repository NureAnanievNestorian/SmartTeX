from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.http import FileResponse, HttpRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from accounts.auth_helpers import get_api_user
from projects.services import pdf_relative_url, pdf_version
from projects.views import _project_with_owner

from .services import (
    LongdocAccessError,
    assert_longdoc_feature,
    create_annotation,
    create_context_file,
    create_note_section,
    create_outline_item,
    create_requirement,
    create_task,
    delete_annotation,
    delete_context_file,
    delete_note_section,
    delete_outline_item,
    delete_task,
    get_context_file,
    get_or_create_longdoc_settings,
    get_section_summary,
    list_annotations,
    list_diff_annotations,
    list_context_files,
    list_note_sections,
    list_outline_items,
    list_requirements,
    list_section_summaries,
    list_tasks,
    overview_payload,
    serialize_annotation,
    serialize_diff_annotation,
    serialize_settings,
    sync_context_file_records,
    update_annotation,
    update_context_file,
    update_longdoc_settings,
    update_note_section,
    update_small_model_settings,
    update_outline_item,
    update_requirement,
    update_section_summary,
    update_task,
)
from .locks import get_locking_change_proposal, get_locking_session
from .models import ChangeProposalDiffAnnotation


def _json_body(request: HttpRequest) -> dict:
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


def _unauthorized() -> JsonResponse:
    return JsonResponse({"detail": "Authentication required"}, status=401)


def _change_meta(request: HttpRequest, body: dict | None = None) -> dict:
    body = body or {}
    source = (
        request.headers.get("X-Change-Source")
        or body.get("change_source")
        or "web"
    ).strip().lower()
    summary = str(
        request.headers.get("X-Change-Summary")
        or body.get("change_summary")
        or ""
    ).strip()
    if source == "mcp" and not summary:
        raise ValueError("change_summary is required for MCP edits")
    if source not in {"mcp", "web", "api"}:
        source = "web"
    return {"source": source, "summary": summary}


def _serialize_settings_for_project(project) -> dict:
    settings_obj, _ = get_or_create_longdoc_settings(project)
    locking_proposal = get_locking_change_proposal(project)
    locking_session = get_locking_session(project)
    return serialize_settings(
        settings_obj,
        locked=locking_proposal is not None or locking_session is not None,
        locking_session_id=locking_session.id if locking_session else None,
        locking_proposal_id=locking_proposal.id if locking_proposal else None,
    )


def _error_response(exc: Exception) -> JsonResponse:
    if isinstance(exc, LongdocAccessError):
        return JsonResponse(exc.payload(), status=exc.status_code)
    if isinstance(exc, ValueError):
        return JsonResponse({"detail": str(exc)}, status=400)
    raise exc


def _proposal_error_response(exc: Exception) -> JsonResponse:
    from longdoc.locks import ProjectLockedError
    from longdoc.session_service import SessionWriteError

    if isinstance(exc, SessionWriteError):
        return JsonResponse(exc.payload(), status=exc.status_code)
    if isinstance(exc, ProjectLockedError):
        return JsonResponse(
            {
                "error": "PROJECT_LOCKED",
                "message": str(exc),
                "suggestion": exc.suggestion(),
                "session_id": exc.session.id,
            },
            status=423,
        )
    return _error_response(exc)


def _service_changes(body: dict) -> dict:
    return {
        key: value
        for key, value in body.items()
        if key not in {"change_summary", "change_source"}
    }


def _active_proposal_for_api(project):
    from longdoc.proposal_service import get_active_change_proposal

    proposal = get_active_change_proposal(project)
    if proposal is None:
        raise LongdocAccessError(
            error="NO_ACTIVE_PROPOSAL",
            message="No active suggested change.",
            status_code=404,
            suggestion="Create or open an active proposal before working with diff annotations.",
        )
    return proposal


@csrf_exempt
@require_http_methods(["GET", "PATCH"])
def api_longdoc_settings(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)

    if request.method == "GET":
        return JsonResponse(_serialize_settings_for_project(project))

    body = _json_body(request)
    changes = {}
    for key in (
        "enabled",
        "context_enabled",
        "outline_enabled",
        "tasks_enabled",
        "annotations_enabled",
        "notes_enabled",
        "summaries_enabled",
        "requirements_enabled",
        "ai_sessions_enabled",
        "mcp_controlled_access",
        "mcp_write_context",
    ):
        if key in body:
            changes[key] = bool(body[key])
    smcl_changes = {}
    for key in (
        "small_model_control_enabled",
        "context_compressor_enabled",
        "edit_intent_classifier_enabled",
        "diff_safety_reviewer_enabled",
        "compile_log_triage_enabled",
        "circuit_breaker_enabled",
        "nav_index_enrich_enabled",
        "nav_rerank_enabled",
        "nav_repair_enabled",
    ):
        if key in body:
            smcl_changes[key] = bool(body[key])
    try:
        update_longdoc_settings(project, **changes)
        if smcl_changes:
            update_small_model_settings(project, **smcl_changes)
    except Exception as exc:
        return _error_response(exc)
    return JsonResponse(_serialize_settings_for_project(project))


@csrf_exempt
@require_http_methods(["GET"])
def api_longdoc_overview(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    try:
        assert_longdoc_feature(project, "enabled")
    except Exception as exc:
        return _error_response(exc)
    return JsonResponse(overview_payload(project))


@csrf_exempt
@require_http_methods(["GET"])
def api_project_ai_request_log(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    from small_model.models import SmallModelUsageLog

    limit = min(max(int(request.GET.get("limit", "40") or 40), 1), 100)
    rows = list(
        SmallModelUsageLog.objects.filter(project=project)
        .order_by("-created_at")[:limit]
        .values(
            "created_at",
            "task_type",
            "status",
            "provider",
            "model_name",
            "input_tokens_estimate",
            "output_tokens_estimate",
            "latency_ms",
            "error_code",
            "input_prompt",
            "output_text",
        )
    )
    summary = {
        "total_requests": SmallModelUsageLog.objects.filter(project=project).count(),
        "total_input_tokens": SmallModelUsageLog.objects.filter(project=project).aggregate(v=Sum("input_tokens_estimate"))["v"] or 0,
        "total_output_tokens": SmallModelUsageLog.objects.filter(project=project).aggregate(v=Sum("output_tokens_estimate"))["v"] or 0,
        "by_status": list(
            SmallModelUsageLog.objects.filter(project=project)
            .values("status")
            .annotate(count=Count("id"))
            .order_by("-count", "status")
        ),
        "by_task": list(
            SmallModelUsageLog.objects.filter(project=project)
            .values("task_type")
            .annotate(count=Count("id"))
            .order_by("-count", "task_type")
        ),
    }
    return JsonResponse({"summary": summary, "items": rows})


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_context_files(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)

    if request.method == "GET":
        try:
            assert_longdoc_feature(project, "context_enabled")
        except Exception as exc:
            return _error_response(exc)
        return JsonResponse({"context_files": list_context_files(project)})

    body = _json_body(request)
    try:
        meta = _change_meta(request, body)
        assert_longdoc_feature(project, "context_enabled", require_write=True)
        item = create_context_file(
            project,
            filename=str(body.get("filename") or "").strip(),
            content=str(body.get("content") or ""),
            description=str(body.get("description") or ""),
            display_name=str(body.get("display_name") or ""),
            actor=user,
            source=meta["source"],
            summary=meta["summary"],
            is_read_only=bool(body.get("is_read_only", False)),
        )
    except Exception as exc:
        return _error_response(exc)
    return JsonResponse(item, status=201)


@csrf_exempt
@require_http_methods(["GET", "PATCH", "DELETE"])
def api_context_file_detail(request: HttpRequest, project_id: int, filename: str) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)

    if request.method == "GET":
        try:
            assert_longdoc_feature(project, "context_enabled")
            include_content = str(request.GET.get("include_content", "true")).lower() != "false"
            return JsonResponse(get_context_file(project, filename, include_content=include_content))
        except Exception as exc:
            return _error_response(exc)

    body = _json_body(request)
    try:
        meta = _change_meta(request, body)
        assert_longdoc_feature(project, "context_enabled", require_write=True)
        if request.method == "DELETE":
            delete_context_file(project, filename=filename, actor=user, source=meta["source"], summary=meta["summary"])
            return JsonResponse({}, status=204)
        item = update_context_file(
            project,
            filename=filename,
            content=body["content"] if "content" in body else None,
            description=body["description"] if "description" in body else None,
            display_name=body["display_name"] if "display_name" in body else None,
            is_read_only=body["is_read_only"] if "is_read_only" in body else None,
            actor=user,
            source=meta["source"],
            summary=meta["summary"],
            create_if_missing=bool(body.get("create_if_missing", False)),
        )
        return JsonResponse(item)
    except Exception as exc:
        return _error_response(exc)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_outline_items(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)

    if request.method == "GET":
        try:
            assert_longdoc_feature(project, "outline_enabled")
        except Exception as exc:
            return _error_response(exc)
        return JsonResponse({"outline_items": list_outline_items(project)})

    body = _json_body(request)
    try:
        meta = _change_meta(request, body)
        assert_longdoc_feature(project, "outline_enabled", require_write=True)
        item = create_outline_item(
            project,
            actor=user,
            source=meta["source"],
            summary=meta["summary"],
            title=str(body.get("title") or "").strip(),
            level=int(body.get("level", 1)),
            status=str(body.get("status") or "missing"),
            order=int(body["order"]) if "order" in body and body["order"] is not None else None,
            notes=str(body.get("notes") or ""),
            expected_pages=body.get("expected_pages"),
            parent_id=body.get("parent_id"),
        )
    except Exception as exc:
        return _error_response(exc)
    return JsonResponse(item, status=201)


@csrf_exempt
@require_http_methods(["PATCH", "DELETE"])
def api_outline_item_detail(request: HttpRequest, project_id: int, item_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    body = _json_body(request)
    try:
        meta = _change_meta(request, body)
        assert_longdoc_feature(project, "outline_enabled", require_write=True)
        if request.method == "DELETE":
            delete_outline_item(project, item_id=item_id, actor=user, source=meta["source"], summary=meta["summary"])
            return JsonResponse({}, status=204)
        item = update_outline_item(
            project,
            item_id=item_id,
            actor=user,
            source=meta["source"],
            summary=meta["summary"],
            **_service_changes(body),
        )
        return JsonResponse(item)
    except Exception as exc:
        return _error_response(exc)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_longdoc_tasks(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)

    if request.method == "GET":
        try:
            assert_longdoc_feature(project, "tasks_enabled")
        except Exception as exc:
            return _error_response(exc)
        return JsonResponse({"tasks": list_tasks(project)})

    body = _json_body(request)
    try:
        meta = _change_meta(request, body)
        assert_longdoc_feature(project, "tasks_enabled", require_write=True)
        item = create_task(
            project,
            description=str(body.get("description") or "").strip(),
            actor=user,
            source=meta["source"],
            summary=meta["summary"],
        )
    except Exception as exc:
        return _error_response(exc)
    return JsonResponse(item, status=201)


@csrf_exempt
@require_http_methods(["PATCH", "DELETE"])
def api_longdoc_task_detail(request: HttpRequest, project_id: int, task_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    body = _json_body(request)
    try:
        meta = _change_meta(request, body)
        assert_longdoc_feature(project, "tasks_enabled", require_write=True)
        if request.method == "DELETE":
            delete_task(project, task_id=task_id, actor=user, source=meta["source"], summary=meta["summary"])
            return JsonResponse({}, status=204)
        item = update_task(
            project,
            task_id=task_id,
            actor=user,
            source=meta["source"],
            summary=meta["summary"],
            **_service_changes(body),
        )
        return JsonResponse(item)
    except Exception as exc:
        return _error_response(exc)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_note_sections(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)

    if request.method == "GET":
        try:
            assert_longdoc_feature(project, "notes_enabled")
        except Exception as exc:
            return _error_response(exc)
        compact = str(request.GET.get("compact", "false")).lower() == "true"
        return JsonResponse({"note_sections": list_note_sections(project, include_preview=compact)})

    body = _json_body(request)
    try:
        meta = _change_meta(request, body)
        assert_longdoc_feature(project, "notes_enabled", require_write=True)
        item = create_note_section(
            project,
            heading=str(body.get("heading") or "").strip(),
            body=str(body.get("body") or ""),
            order=int(body["order"]) if "order" in body and body["order"] is not None else None,
            actor=user,
            source=meta["source"],
            summary=meta["summary"],
        )
    except Exception as exc:
        return _error_response(exc)
    return JsonResponse(item, status=201)


@csrf_exempt
@require_http_methods(["PATCH", "DELETE"])
def api_note_section_detail(request: HttpRequest, project_id: int, section_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    body = _json_body(request)
    try:
        meta = _change_meta(request, body)
        assert_longdoc_feature(project, "notes_enabled", require_write=True)
        if request.method == "DELETE":
            delete_note_section(project, section_id=section_id, actor=user, source=meta["source"], summary=meta["summary"])
            return JsonResponse({}, status=204)
        item = update_note_section(
            project,
            section_id=section_id,
            actor=user,
            source=meta["source"],
            summary=meta["summary"],
            **_service_changes(body),
        )
        return JsonResponse(item)
    except Exception as exc:
        return _error_response(exc)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_annotations(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)

    if request.method == "GET":
        try:
            assert_longdoc_feature(project, "annotations_enabled")
        except Exception as exc:
            return _error_response(exc)
        status = str(request.GET.get("status") or "").strip() or None
        file_name = str(request.GET.get("file_name") or "").strip() or None
        return JsonResponse({"annotations": list_annotations(project, status=status, file_name=file_name)})

    body = _json_body(request)
    try:
        meta = _change_meta(request, body)
        assert_longdoc_feature(project, "annotations_enabled", require_write=True)
        item = create_annotation(
            project,
            file_name=str(body.get("file_name") or "").strip(),
            instruction=str(body.get("instruction") or ""),
            line_start=int(body["line_start"]) if body.get("line_start") is not None else None,
            line_end=int(body["line_end"]) if body.get("line_end") is not None else None,
            selected_text=str(body.get("selected_text") or ""),
            task_id=int(body["task_id"]) if body.get("task_id") is not None and str(body.get("task_id")).strip() != "" else None,
            actor=user,
            source=meta["source"],
            summary=meta["summary"],
            status=str(body.get("status") or "").strip() or None,
        )
    except Exception as exc:
        return _error_response(exc)
    return JsonResponse(item, status=201)


@csrf_exempt
@require_http_methods(["PATCH", "DELETE"])
def api_annotation_detail(request: HttpRequest, project_id: int, annotation_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    body = _json_body(request)
    try:
        meta = _change_meta(request, body)
        assert_longdoc_feature(project, "annotations_enabled", require_write=True)
        if request.method == "DELETE":
            delete_annotation(project, annotation_id=annotation_id, actor=user, source=meta["source"], summary=meta["summary"])
            return JsonResponse({}, status=204)
        item = update_annotation(
            project,
            annotation_id=annotation_id,
            actor=user,
            source=meta["source"],
            summary=meta["summary"],
            **_service_changes(body),
        )
        return JsonResponse(item)
    except Exception as exc:
        return _error_response(exc)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_section_summaries(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)

    if request.method == "GET":
        try:
            assert_longdoc_feature(project, "summaries_enabled")
        except Exception as exc:
            return _error_response(exc)
        title = str(request.GET.get("section_title") or "").strip()
        if title:
            try:
                return JsonResponse(get_section_summary(project, section_title=title))
            except Exception as exc:
                return _error_response(exc)
        return JsonResponse({"section_summaries": list_section_summaries(project)})

    body = _json_body(request)
    try:
        meta = _change_meta(request, body)
        assert_longdoc_feature(project, "summaries_enabled", require_write=True)
        item = update_section_summary(
            project,
            section_title=str(body.get("section_title") or "").strip(),
            summary_text=str(body.get("summary_text") or ""),
            section_index=int(body["section_index"]) if body.get("section_index") is not None else None,
            source_file=str(body.get("source_file") or "") or None,
            source_line_start=int(body["source_line_start"]) if body.get("source_line_start") is not None else None,
            source_line_end=int(body["source_line_end"]) if body.get("source_line_end") is not None else None,
            actor=user,
            source=meta["source"],
            summary=meta["summary"],
        )
        return JsonResponse(item, status=201)
    except Exception as exc:
        return _error_response(exc)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_requirements(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)

    if request.method == "GET":
        try:
            assert_longdoc_feature(project, "requirements_enabled")
        except Exception as exc:
            return _error_response(exc)
        return JsonResponse({"requirements": list_requirements(project)})

    body = _json_body(request)
    try:
        meta = _change_meta(request, body)
        assert_longdoc_feature(project, "requirements_enabled", require_write=True)
        item = create_requirement(
            project,
            req_id=str(body.get("req_id") or "").strip(),
            description=str(body.get("description") or "").strip(),
            coverage=str(body.get("coverage") or "unchecked"),
            notes=str(body.get("notes") or ""),
            section_refs=list(body.get("section_refs") or []),
            actor=user,
            source=meta["source"],
            summary=meta["summary"],
        )
        return JsonResponse(item, status=201)
    except Exception as exc:
        return _error_response(exc)


@csrf_exempt
@require_http_methods(["PATCH"])
def api_requirement_detail(request: HttpRequest, project_id: int, requirement_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    body = _json_body(request)
    try:
        meta = _change_meta(request, body)
        assert_longdoc_feature(project, "requirements_enabled", require_write=True)
        item = update_requirement(
            project,
            requirement_id=requirement_id,
            actor=user,
            source=meta["source"],
            summary=meta["summary"],
            **_service_changes(body),
        )
        return JsonResponse(item)
    except Exception as exc:
        return _error_response(exc)


# ── Change proposal endpoints ───────────────────────────────────────────────


def _serialize_proposal_response(project) -> dict:
    from longdoc.proposal_service import get_active_change_proposal, serialize_change_proposal

    return {"proposal": serialize_change_proposal(get_active_change_proposal(project))}



@csrf_exempt
@require_POST
def api_validate_document_change(request: HttpRequest, project_id: int) -> JsonResponse:
    from longdoc.proposal_service import validate_document_change

    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    body = _json_body(request)
    settings_obj, _ = get_or_create_longdoc_settings(project)
    if not (settings_obj.enabled and settings_obj.ai_sessions_enabled):
        return JsonResponse(
            {
                "error": "FEATURE_DISABLED",
                "message": "Suggested changes (proposal workflow) are disabled for this project.",
                "suggestion": (
                    "Do not call propose_document_change or validate_document_change for this project. "
                    "Apply edits directly with update_project_section, replace_in_project_file, "
                    "patch_file_lines, append_to_file, update_project_file, or create_project_text_file."
                ),
                "longdoc_enabled": settings_obj.enabled,
                "ai_sessions_enabled": settings_obj.ai_sessions_enabled,
            },
            status=403,
        )
    try:
        return JsonResponse(
            validate_document_change(
                project,
                goal=str(body.get("goal") or "").strip(),
                patch_ops=list(body.get("patch_ops") or []),
                compile_preview=bool(body.get("compile", True)),
                auto_reorder_line_patches=bool(body.get("auto_reorder_line_patches", True)),
            )
        )
    except Exception as exc:
        return _proposal_error_response(exc)

@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_change_proposals(request: HttpRequest, project_id: int) -> JsonResponse:
    from longdoc.proposal_service import propose_document_change, serialize_change_proposal
    from longdoc.session_service import SessionWriteError

    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)

    if request.method == "GET":
        return JsonResponse(_serialize_proposal_response(project))

    body = _json_body(request)
    settings_obj, _ = get_or_create_longdoc_settings(project)
    if not (settings_obj.enabled and settings_obj.ai_sessions_enabled):
        return JsonResponse(
            {
                "error": "FEATURE_DISABLED",
                "message": "Suggested changes (proposal workflow) are disabled for this project.",
                "suggestion": (
                    "Do not call propose_document_change for this project. "
                    "Apply edits directly with update_project_section, replace_in_project_file, "
                    "patch_file_lines, append_to_file, update_project_file, or create_project_text_file."
                ),
                "longdoc_enabled": settings_obj.enabled,
                "ai_sessions_enabled": settings_obj.ai_sessions_enabled,
            },
            status=403,
        )
    try:
        raw_patch_ops = body.get("patch_ops")
        proposal = propose_document_change(
            project,
            goal=str(body.get("goal") or "").strip(),
            patch_ops=list(raw_patch_ops) if raw_patch_ops else None,
            addresses_task_id=body.get("addresses_task_id"),
            addresses_outline_item_id=body.get("addresses_outline_item_id"),
            annotation_ids=list(body.get("annotation_ids") or []),
            diff_annotation_ids=list(body.get("diff_annotation_ids") or []),
            validation_token=(str(body.get("validation_token")) if body.get("validation_token") else None),
            continue_existing=bool(body.get("continue_existing", False)),
        )
    except Exception as exc:
        return _proposal_error_response(exc)
    status = 201
    return JsonResponse({"proposal": serialize_change_proposal(proposal)}, status=status)


@csrf_exempt
@require_http_methods(["GET"])
def api_change_proposal_status(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    return JsonResponse(_serialize_proposal_response(project))


@csrf_exempt
@require_POST
def api_change_proposal_cancel(request: HttpRequest, project_id: int) -> JsonResponse:
    from longdoc.proposal_service import cancel_change_proposal, get_active_change_proposal, serialize_change_proposal

    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    proposal = get_active_change_proposal(project)
    if proposal is None:
        return JsonResponse({"error": "NO_ACTIVE_PROPOSAL", "message": "No active suggested change."}, status=404)
    try:
        proposal = cancel_change_proposal(proposal)
    except Exception as exc:
        return _proposal_error_response(exc)
    return JsonResponse({"proposal": serialize_change_proposal(proposal)})


@csrf_exempt
@require_http_methods(["GET"])
def api_change_proposal_diff(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    try:
        proposal = _active_proposal_for_api(project)
    except Exception as exc:
        return _proposal_error_response(exc)
    resolved_annotations = []
    session = proposal.internal_session
    batch = getattr(session, "batch", None) if session is not None else None
    if batch is not None:
        resolved_annotations = [
            serialize_annotation(item)
            for item in batch.annotations_completed.order_by("file_name", "line_start", "id")
        ]
    return JsonResponse(
        {
            "diff_text": proposal.diff_summary,
            "proposal_id": proposal.id,
            "status": proposal.status,
            "compile_error_summary": proposal.compile_error_summary or "",
            "smcl_risk_level": proposal.smcl_risk_level or "low",
            "smcl_warnings": proposal.smcl_warnings or [],
            "resolved_annotations": resolved_annotations,
            "diff_annotations": list_diff_annotations(proposal),
        }
    )


@csrf_exempt
@require_POST
def api_change_proposal_manual_edit(request: HttpRequest, project_id: int) -> JsonResponse:
    from longdoc.models import AISession, ChangeProposal
    from longdoc.proposal_service import _serialize_compile_failure, serialize_change_proposal
    from longdoc.session_service import (
        SessionWriteError,
        compile_session,
        finalize_batch,
        generate_diff,
        manually_edit_session_line,
    )

    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    try:
        proposal = _active_proposal_for_api(project)
    except Exception as exc:
        return _proposal_error_response(exc)
    session = proposal.internal_session
    if session is None:
        return JsonResponse({"error": "NO_SESSION", "message": "This proposal has no editable session."}, status=404)
    if proposal.status not in (ChangeProposal.Status.READY_FOR_REVIEW, ChangeProposal.Status.FAILED_COMPILE):
        return JsonResponse(
            {"error": "PROPOSAL_NOT_EDITABLE", "message": "Only reviewable proposal diffs can be manually edited."},
            status=409,
        )

    body = _json_body(request)
    try:
        result = manually_edit_session_line(
            session,
            str(body.get("file_name") or ""),
            line_number=int(body.get("line_number") or 0),
            new_text=str(body.get("new_text") or ""),
            expected_text=str(body["expected_text"]) if "expected_text" in body else None,
        )
        if result.get("changed"):
            session.refresh_from_db()
            session.status = AISession.Status.ACTIVE
            session.save(update_fields=["status", "updated_at"])
            compile_result = compile_session(session)
            session.refresh_from_db()
            proposal.compile_status = session.compile_status
            proposal.diff_summary = generate_diff(session)
            if compile_result.get("status") == "success" and session.compile_status == AISession.CompileStatus.SUCCESS:
                previous_annotation_ids = []
                if hasattr(session, "batch"):
                    previous_annotation_ids = list(session.batch.annotations_completed.values_list("id", flat=True))
                batch = finalize_batch(
                    session,
                    summary=proposal.goal,
                    task_ids=[proposal.addresses_task_id] if proposal.addresses_task_id else None,
                    annotation_ids=previous_annotation_ids,
                )
                proposal.status = ChangeProposal.Status.READY_FOR_REVIEW
                proposal.compile_error_summary = ""
                proposal.changed_files = [
                    {
                        "filename": change.filename,
                        "change_type": change.change_type,
                        "lines_added": change.lines_added,
                        "lines_removed": change.lines_removed,
                    }
                    for change in batch.changes.order_by("filename", "id")
                ]
                proposal.user_visible_message = "Ready for review with manual edits"
            else:
                proposal.status = ChangeProposal.Status.FAILED_COMPILE
                proposal.compile_error_summary = _serialize_compile_failure(compile_result)
                proposal.changed_files = []
                proposal.user_visible_message = "Manual edit applied, but the proposal no longer compiles."
            proposal.save(
                update_fields=[
                    "status",
                    "compile_status",
                    "compile_error_summary",
                    "diff_summary",
                    "changed_files",
                    "user_visible_message",
                    "updated_at",
                ]
            )
    except SessionWriteError as exc:
        return JsonResponse(exc.payload(), status=exc.status_code)
    except Exception as exc:
        logger.exception("manual proposal edit failed for project %s", project_id)
        return JsonResponse({"error": "MANUAL_EDIT_FAILED", "message": str(exc)}, status=500)

    proposal.refresh_from_db()
    return JsonResponse(
        {
            "proposal": serialize_change_proposal(proposal),
            "result": result,
        }
    )


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_change_proposal_diff_annotations(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    try:
        proposal = _active_proposal_for_api(project)
    except Exception as exc:
        return _proposal_error_response(exc)

    if request.method == "GET":
        status = str(request.GET.get("status") or "").strip() or None
        return JsonResponse({"diff_annotations": list_diff_annotations(proposal, status=status)})

    body = _json_body(request)
    try:
        meta = _change_meta(request, body)
        instruction = str(body.get("instruction") or "").strip()
        file_name = str(body.get("file_name") or "").strip()
        side = str(body.get("side") or ChangeProposalDiffAnnotation.Side.NEW).strip()
        if side not in {choice[0] for choice in ChangeProposalDiffAnnotation.Side.choices}:
            side = ChangeProposalDiffAnnotation.Side.NEW
        if not instruction:
            raise ValueError("instruction is required")
        if not file_name:
            raise ValueError("file_name is required")
        item = ChangeProposalDiffAnnotation.objects.create(
            proposal=proposal,
            file_name=file_name,
            side=side,
            line_number=max(1, int(body.get("line_number") or 1)),
            selected_text=str(body.get("selected_text") or ""),
            instruction=instruction,
            created_by=(
                ChangeProposalDiffAnnotation.CreatedBy.MCP
                if meta["source"] == "mcp"
                else ChangeProposalDiffAnnotation.CreatedBy.USER
            ),
        )
    except Exception as exc:
        return _proposal_error_response(exc)
    return JsonResponse(serialize_diff_annotation(item), status=201)


@csrf_exempt
@require_http_methods(["PATCH", "DELETE"])
def api_change_proposal_diff_annotation_detail(request: HttpRequest, project_id: int, annotation_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    try:
        proposal = _active_proposal_for_api(project)
        item = ChangeProposalDiffAnnotation.objects.get(proposal=proposal, id=annotation_id)
        if request.method == "DELETE":
            item.delete()
            return JsonResponse({}, status=204)
        body = _json_body(request)
        _change_meta(request, body)
        changes = _service_changes(body)
        if "instruction" in changes:
            item.instruction = str(changes.pop("instruction") or "").strip()
            if not item.instruction:
                raise ValueError("instruction is required")
        if "selected_text" in changes:
            item.selected_text = str(changes.pop("selected_text") or "")
        if "file_name" in changes:
            item.file_name = str(changes.pop("file_name") or "").strip()
            if not item.file_name:
                raise ValueError("file_name is required")
        if "line_number" in changes:
            item.line_number = max(1, int(changes.pop("line_number") or 1))
        if "side" in changes:
            side = str(changes.pop("side") or "").strip()
            if side not in {choice[0] for choice in ChangeProposalDiffAnnotation.Side.choices}:
                raise ValueError("unsupported diff annotation side")
            item.side = side
        if "status" in changes:
            status = str(changes.pop("status") or "").strip()
            if status not in {choice[0] for choice in ChangeProposalDiffAnnotation.Status.choices}:
                raise ValueError("unsupported diff annotation status")
            item.status = status
            if status in (ChangeProposalDiffAnnotation.Status.DONE, ChangeProposalDiffAnnotation.Status.DISMISSED):
                item.resolved_at = timezone.now()
                if status == ChangeProposalDiffAnnotation.Status.DONE and proposal.internal_session_id:
                    item.resolved_by_session = proposal.internal_session
            else:
                item.resolved_at = None
                item.resolved_by_session = None
        if changes:
            raise ValueError(f"unsupported diff annotation changes: {', '.join(sorted(changes))}")
        item.save()
    except ChangeProposalDiffAnnotation.DoesNotExist:
        return JsonResponse({"error": "DIFF_ANNOTATION_NOT_FOUND", "message": "Diff annotation not found."}, status=404)
    except Exception as exc:
        return _proposal_error_response(exc)
    return JsonResponse(serialize_diff_annotation(item))


@csrf_exempt
@require_http_methods(["GET"])
def api_change_proposal_preview_pdf(request: HttpRequest, project_id: int):
    from longdoc.proposal_service import get_active_change_proposal
    from projects.services import project_dir as get_project_dir

    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    proposal = get_active_change_proposal(project)
    session = proposal.internal_session if proposal else None
    if session is None or not session.staging_pdf_path:
        return JsonResponse({"error": "NO_PREVIEW_PDF", "message": "No preview PDF available."}, status=404)
    pdf_path = get_project_dir(project) / session.staging_pdf_path
    if not pdf_path.exists():
        return JsonResponse({"error": "NO_PREVIEW_PDF", "message": "Preview PDF file not found."}, status=404)
    return FileResponse(open(pdf_path, "rb"), content_type="application/pdf")


@login_required
@csrf_exempt
@require_POST
def api_change_proposal_accept(request: HttpRequest, project_id: int) -> JsonResponse:
    from longdoc.models import ChangeProposal
    from longdoc.proposal_service import get_active_change_proposal, serialize_change_proposal
    from longdoc.session_service import SessionWriteError, accept_session

    project = _project_with_owner(project_id, request.user)
    proposal = get_active_change_proposal(project)
    body = _json_body(request)
    accept_compile_errors = bool(body.get("accept_compile_errors", False))
    if proposal is None or proposal.internal_session is None:
        return JsonResponse({"error": "NO_ACTIVE_PROPOSAL", "message": "No active suggested change."}, status=404)
    if proposal.status == ChangeProposal.Status.FAILED_COMPILE and not accept_compile_errors:
        return JsonResponse(
            {
                "error": "COMPILE_FAILED_REVIEW_REQUIRED",
                "message": "This proposal has compile errors and requires explicit override to accept.",
                "warning_code": "ACCEPT_COMPILE_ERRORS_REQUIRED",
                "suggestion": "Retry with accept_compile_errors=true only if the user explicitly wants to accept non-compiling changes.",
            },
            status=409,
        )
    allowed_statuses = {ChangeProposal.Status.READY_FOR_REVIEW}
    if accept_compile_errors:
        allowed_statuses.add(ChangeProposal.Status.FAILED_COMPILE)
    if proposal.status not in allowed_statuses:
        return JsonResponse(
            {"error": "PROPOSAL_NOT_READY", "message": "Only ready-for-review suggestions can be accepted."},
            status=409,
        )
    try:
        accept_session(proposal.internal_session, user=request.user, allow_with_compile_errors=accept_compile_errors)
    except SessionWriteError as exc:
        return JsonResponse(exc.payload(), status=exc.status_code)
    except Exception as exc:
        logger.exception("accept_session failed for project %s", project_id)
        return JsonResponse({"error": "ACCEPT_FAILED", "message": str(exc)}, status=500)
    proposal.refresh_from_db()
    project.refresh_from_db()
    payload = {
        "proposal": serialize_change_proposal(proposal),
        "pdf_url": pdf_relative_url(project),
        "pdf_version": pdf_version(project),
        "project": {"last_status": project.last_status},
    }
    if accept_compile_errors:
        payload["warning"] = {
            "code": "ACCEPTED_WITH_COMPILE_ERRORS",
            "message": "Accepted despite compile errors by explicit override.",
        }
    return JsonResponse(payload)


@login_required
@csrf_exempt
@require_POST
def api_change_proposal_discard(request: HttpRequest, project_id: int) -> JsonResponse:
    from longdoc.proposal_service import get_active_change_proposal, serialize_change_proposal
    from longdoc.session_service import SessionWriteError, discard_session

    project = _project_with_owner(project_id, request.user)
    proposal = get_active_change_proposal(project)
    if proposal is None:
        return JsonResponse({"error": "NO_ACTIVE_PROPOSAL", "message": "No active suggested change."}, status=404)
    try:
        if proposal.internal_session_id:
            discard_session(proposal.internal_session)
        else:
            proposal.status = proposal.Status.DISCARDED
            proposal.discarded_at = timezone.now()
            proposal.user_visible_message = "Discarded"
            proposal.save(update_fields=["status", "discarded_at", "user_visible_message", "updated_at"])
    except SessionWriteError as exc:
        return JsonResponse(exc.payload(), status=exc.status_code)
    except Exception as exc:
        return JsonResponse({"error": "DISCARD_FAILED", "message": str(exc)}, status=500)
    proposal.refresh_from_db()
    return JsonResponse({"proposal": serialize_change_proposal(proposal)})


@csrf_exempt
@require_http_methods(["GET"])
def api_document_graph(request: HttpRequest, project_id: int) -> JsonResponse:
    from longdoc.document_graph import inspect_document_graph

    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    try:
        return JsonResponse(inspect_document_graph(project).as_dict())
    except Exception as exc:
        return JsonResponse({"error": "GRAPH_INSPECTION_FAILED", "message": str(exc)}, status=500)


@csrf_exempt
@require_POST
def api_preview_patch(request: HttpRequest, project_id: int) -> JsonResponse:
    from longdoc.proposal_service import preview_patch

    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    try:
        return JsonResponse(preview_patch(project, _json_body(request)))
    except Exception as exc:
        return _proposal_error_response(exc)


# ── MCP read-budget tracking ────────────────────────────────────────────────


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_mcp_budget(request: HttpRequest, project_id: int) -> JsonResponse:
    """Store and retrieve per-(token, project) MCP read-budget in Django cache.

    GET  → {remaining: N}
    POST {lines: N} → {remaining: N, over_budget: bool}
    """
    from django.core.cache import cache

    user = get_api_user(request)
    if not user:
        return _unauthorized()
    _project_with_owner(project_id, user)  # ownership check only

    import hashlib as _hashlib
    from django.conf import settings as _settings

    max_budget = int(getattr(_settings, "MCP_SESSION_READ_BUDGET", 2000))
    auth_header = (request.headers.get("Authorization") or "").strip()
    token = auth_header.removeprefix("Bearer ").removeprefix("Token ").strip() or "anon"
    cache_key = f"mcp_budget:{_hashlib.sha256(token.encode()).hexdigest()[:16]}:{project_id}"

    if request.method == "GET":
        remaining = cache.get(cache_key, max_budget)
        return JsonResponse({"remaining": remaining})

    body = _json_body(request)
    lines = max(0, int(body.get("lines") or 0))
    current = cache.get(cache_key, max_budget)
    new_remaining = max(0, current - lines)
    cache.set(cache_key, new_remaining, timeout=3600)
    return JsonResponse({"remaining": new_remaining, "over_budget": new_remaining == 0 and lines > 0})
