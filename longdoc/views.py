from __future__ import annotations

import json
from pathlib import Path

from django.contrib.auth.decorators import login_required
from django.http import FileResponse, HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from accounts.auth_helpers import get_api_user
from projects.views import _project_with_owner

from .services import (
    LongdocAccessError,
    assert_longdoc_feature,
    create_context_file,
    create_note_section,
    create_outline_item,
    create_requirement,
    create_task,
    delete_context_file,
    delete_note_section,
    delete_outline_item,
    delete_task,
    get_context_file,
    get_or_create_longdoc_settings,
    get_section_summary,
    list_context_files,
    list_note_sections,
    list_outline_items,
    list_requirements,
    list_section_summaries,
    list_tasks,
    overview_payload,
    serialize_settings,
    sync_context_file_records,
    update_context_file,
    update_longdoc_settings,
    update_note_section,
    update_outline_item,
    update_requirement,
    update_section_summary,
    update_task,
)
from .locks import get_locking_session


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
    locking_session = get_locking_session(project)
    return serialize_settings(
        settings_obj,
        locked=locking_session is not None,
        locking_session_id=locking_session.id if locking_session else None,
    )


def _error_response(exc: Exception) -> JsonResponse:
    if isinstance(exc, LongdocAccessError):
        return JsonResponse(exc.payload(), status=exc.status_code)
    if isinstance(exc, ValueError):
        return JsonResponse({"detail": str(exc)}, status=400)
    raise exc


def _service_changes(body: dict) -> dict:
    return {
        key: value
        for key, value in body.items()
        if key not in {"change_summary", "change_source"}
    }


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
        "notes_enabled",
        "summaries_enabled",
        "requirements_enabled",
        "ai_sessions_enabled",
        "mcp_controlled_access",
        "mcp_write_context",
    ):
        if key in body:
            changes[key] = bool(body[key])
    try:
        update_longdoc_settings(project, **changes)
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


# ── AI Session endpoints ────────────────────────────────────────────────────


def _serialize_session(session) -> dict:
    batch = None
    try:
        b = session.batch
        batch = {
            "id": b.id,
            "summary": b.summary,
            "notes_updated": b.notes_updated,
            "requirements_updated": b.requirements_updated,
            "tasks_completed": list(b.tasks_completed.values_list("id", flat=True)),
            "changes": [
                {
                    "filename": c.filename,
                    "change_type": c.change_type,
                    "diff_text": c.diff_text,
                    "lines_added": c.lines_added,
                    "lines_removed": c.lines_removed,
                }
                for c in b.changes.all()
            ],
        }
    except Exception:
        pass
    return {
        "id": session.id,
        "goal": session.goal,
        "branch_name": session.branch_name,
        "status": session.status,
        "compile_status": session.compile_status,
        "compile_log": session.compile_log,
        "staging_pdf_path": session.staging_pdf_path,
        "diff_text": session.diff_text,
        "created_by_scope": session.created_by_scope,
        "expires_at": session.expires_at.isoformat(),
        "accepted_at": session.accepted_at.isoformat() if session.accepted_at else None,
        "discarded_at": session.discarded_at.isoformat() if session.discarded_at else None,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
        "batch": batch,
    }


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_ai_session(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return JsonResponse({"detail": "Authentication required"}, status=401)
    project = _project_with_owner(project_id, user)

    if request.method == "GET":
        session = get_locking_session(project)
        if session is None:
            return JsonResponse({"session": None})
        return JsonResponse({"session": _serialize_session(session)})

    # POST — create a new session
    from longdoc.session_service import SessionWriteError, create_session
    from longdoc.locks import ProjectLockedError

    body = _json_body(request)
    goal = str(body.get("goal") or "").strip() or "AI session"
    expires_hours = body.get("expires_hours")
    settings_obj, _ = get_or_create_longdoc_settings(project)
    if not (settings_obj.enabled and settings_obj.ai_sessions_enabled):
        return JsonResponse({"error": "FEATURE_DISABLED", "message": "AI sessions are disabled for this project."}, status=403)
    try:
        kwargs = {}
        if expires_hours is not None:
            kwargs["expires_hours"] = int(expires_hours)
        session = create_session(project, goal, **kwargs)
    except ProjectLockedError as exc:
        return JsonResponse({"error": "PROJECT_LOCKED", "message": str(exc)}, status=423)
    except SessionWriteError as exc:
        return JsonResponse(exc.payload(), status=exc.status_code)
    except Exception as exc:
        return JsonResponse({"error": "CREATE_FAILED", "message": str(exc)}, status=500)
    return JsonResponse({"session": _serialize_session(session)}, status=201)


@csrf_exempt
@require_http_methods(["GET"])
def api_ai_session_diff(request: HttpRequest, project_id: int) -> JsonResponse:
    from longdoc.session_service import generate_diff

    user = get_api_user(request)
    if not user:
        return JsonResponse({"detail": "Authentication required"}, status=401)
    project = _project_with_owner(project_id, user)
    session = get_locking_session(project)
    if session is None:
        return JsonResponse({"error": "NO_ACTIVE_SESSION", "message": "No active session."}, status=404)
    try:
        diff = generate_diff(session)
    except Exception as exc:
        return JsonResponse({"error": "DIFF_FAILED", "message": str(exc)}, status=500)
    return JsonResponse({"diff_text": diff, "session_id": session.id})


@csrf_exempt
@require_http_methods(["GET"])
def api_ai_session_staging_pdf(request: HttpRequest, project_id: int):
    user = get_api_user(request)
    if not user:
        return JsonResponse({"detail": "Authentication required"}, status=401)
    project = _project_with_owner(project_id, user)
    session = get_locking_session(project)
    if session is None or not session.staging_pdf_path:
        return JsonResponse({"error": "NO_STAGING_PDF", "message": "No staging PDF available."}, status=404)
    from projects.services import project_dir as get_project_dir
    pdf_path = get_project_dir(project) / session.staging_pdf_path
    if not pdf_path.exists():
        return JsonResponse({"error": "NO_STAGING_PDF", "message": "Staging PDF file not found."}, status=404)
    return FileResponse(open(pdf_path, "rb"), content_type="application/pdf")


@csrf_exempt
@require_POST
def api_ai_session_finalize(request: HttpRequest, project_id: int) -> JsonResponse:
    from longdoc.session_service import SessionWriteError, finalize_batch

    user = get_api_user(request)
    if not user:
        return JsonResponse({"detail": "Authentication required"}, status=401)
    project = _project_with_owner(project_id, user)
    session = get_locking_session(project)
    if session is None:
        return JsonResponse({"error": "NO_ACTIVE_SESSION", "message": "No active session."}, status=404)
    body = _json_body(request)
    summary = str(body.get("summary") or "").strip()
    task_ids = [int(i) for i in (body.get("task_ids") or [])]
    try:
        batch = finalize_batch(session, summary=summary, task_ids=task_ids or None)
    except SessionWriteError as exc:
        return JsonResponse(exc.payload(), status=exc.status_code)
    except Exception as exc:
        return JsonResponse({"error": "FINALIZE_FAILED", "message": str(exc)}, status=500)
    session.refresh_from_db()
    return JsonResponse({"session": _serialize_session(session), "batch_id": batch.id})


@login_required
@csrf_exempt
@require_POST
def api_ai_session_accept(request: HttpRequest, project_id: int) -> JsonResponse:
    from longdoc.session_service import SessionWriteError, accept_session

    project = _project_with_owner(project_id, request.user)
    session = get_locking_session(project)
    if session is None:
        return JsonResponse({"error": "NO_ACTIVE_SESSION", "message": "No active session."}, status=404)
    try:
        accept_session(session, user=request.user)
    except SessionWriteError as exc:
        return JsonResponse(exc.payload(), status=exc.status_code)
    except Exception as exc:
        return JsonResponse({"error": "ACCEPT_FAILED", "message": str(exc)}, status=500)
    session.refresh_from_db()
    return JsonResponse({"session": _serialize_session(session)})


@login_required
@csrf_exempt
@require_POST
def api_ai_session_discard(request: HttpRequest, project_id: int) -> JsonResponse:
    from longdoc.session_service import SessionWriteError, discard_session

    project = _project_with_owner(project_id, request.user)
    session = get_locking_session(project)
    if session is None:
        return JsonResponse({"error": "NO_ACTIVE_SESSION", "message": "No active session."}, status=404)
    try:
        discard_session(session)
    except SessionWriteError as exc:
        return JsonResponse(exc.payload(), status=exc.status_code)
    except Exception as exc:
        return JsonResponse({"error": "DISCARD_FAILED", "message": str(exc)}, status=500)
    session.refresh_from_db()
    return JsonResponse({"session": _serialize_session(session)})


@csrf_exempt
@require_POST
def api_ai_session_write(request: HttpRequest, project_id: int) -> JsonResponse:
    from longdoc.session_service import SessionWriteError, write_to_session

    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    session = get_locking_session(project)
    if session is None:
        return JsonResponse({"error": "NO_ACTIVE_SESSION", "message": "No active session."}, status=404)
    body = _json_body(request)
    filename = str(body.get("filename") or "").strip()
    op = str(body.get("op") or "").strip()
    change_summary = str(body.get("change_summary") or "").strip()
    if not filename:
        return JsonResponse({"detail": "filename is required"}, status=400)
    if not op:
        return JsonResponse({"detail": "op is required"}, status=400)
    params = {k: v for k, v in body.items() if k not in {"filename", "op", "change_summary"}}
    try:
        result = write_to_session(session, filename, op=op, change_summary=change_summary, **params)
    except SessionWriteError as exc:
        return JsonResponse(exc.payload(), status=exc.status_code)
    except Exception as exc:
        return JsonResponse({"error": "WRITE_FAILED", "message": str(exc)}, status=500)
    return JsonResponse(result)


@csrf_exempt
@require_POST
def api_ai_session_compile(request: HttpRequest, project_id: int) -> JsonResponse:
    from longdoc.session_service import SessionWriteError, compile_session

    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    session = get_locking_session(project)
    if session is None:
        return JsonResponse({"error": "NO_ACTIVE_SESSION", "message": "No active session."}, status=404)
    try:
        result = compile_session(session)
    except SessionWriteError as exc:
        return JsonResponse(exc.payload(), status=exc.status_code)
    except Exception as exc:
        return JsonResponse({"error": "COMPILE_FAILED", "message": str(exc)}, status=500)
    session.refresh_from_db()
    return JsonResponse({"session": _serialize_session(session), **result})


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
