from __future__ import annotations

import asyncio
import json
import re
from http.cookies import SimpleCookie
from typing import Any

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import SESSION_KEY

SSE_PROJECT_UPDATES_RE = re.compile(r"^/sse/projects/(?P<project_id>\d+)/updates/?$")


def _cookie_header(scope: dict[str, Any]) -> str:
    for key, value in scope.get("headers", []):
        if key == b"cookie":
            try:
                return value.decode("utf-8", errors="ignore")
            except Exception:
                return ""
    return ""


def _session_user_id_from_cookie(scope: dict[str, Any]) -> int | None:
    raw_cookie = _cookie_header(scope)
    if not raw_cookie:
        return None
    cookie = SimpleCookie()
    cookie.load(raw_cookie)
    session_name = settings.SESSION_COOKIE_NAME
    morsel = cookie.get(session_name)
    if not morsel:
        return None
    session_key = morsel.value
    if not session_key:
        return None

    module_name = settings.SESSION_ENGINE
    mod = __import__(module_name, fromlist=["SessionStore"])
    SessionStore = getattr(mod, "SessionStore")
    session = SessionStore(session_key=session_key)
    uid = session.get(SESSION_KEY)
    if uid is None:
        return None
    try:
        return int(uid)
    except (TypeError, ValueError):
        return None


def _latest_project_version_for_owner(project_id: int, owner_id: int) -> dict[str, Any] | None:
    from projects.models import ProjectVersion

    row = (
        ProjectVersion.objects.filter(
            project_id=project_id,
            project__owner_id=owner_id,
        )
        .order_by("-id")
        .values("id", "source")
        .first()
    )
    if row is None:
        from projects.models import Project

        exists = Project.objects.filter(id=project_id, owner_id=owner_id).exists()
        if not exists:
            return None
        return {"id": 0, "source": ""}
    return {"id": int(row["id"]), "source": str(row.get("source") or "")}


def _compile_signature_for_owner(project_id: int, owner_id: int) -> dict[str, Any] | None:
    from projects.models import Project
    from projects.services import pdf_file_path

    project = Project.objects.filter(id=project_id, owner_id=owner_id).only("id", "last_status").first()
    if project is None:
        return None
    path = pdf_file_path(project)
    pdf_mtime = int(path.stat().st_mtime_ns) if path.exists() else 0
    return {"status": str(project.last_status or ""), "pdf_mtime": pdf_mtime}


def _compile_event_for_owner(project_id: int, owner_id: int) -> dict[str, Any] | None:
    from projects.models import Project
    from projects.services import (
        compile_state_for_status, has_pdf, pdf_relative_url, pdf_version,
        read_compile_log, parse_compile_diagnostics,
    )

    project = Project.objects.filter(id=project_id, owner_id=owner_id).first()
    if project is None:
        return None
    log = read_compile_log(project)
    return {
        "type": "compile_updated",
        "project_id": project_id,
        "status": str(project.last_status or ""),
        "compile_state": compile_state_for_status(project.last_status or "", request_mode="read"),
        "pdf_url": pdf_relative_url(project) if has_pdf(project) else None,
        "pdf_version": pdf_version(project),
        "log": log,
        "diagnostics": parse_compile_diagnostics(project, log),
    }


def _annotation_signature_for_owner(project_id: int, owner_id: int) -> str | None:
    from longdoc.models import ProjectAnnotation
    from projects.models import Project

    if not Project.objects.filter(id=project_id, owner_id=owner_id).exists():
        return None
    row = (
        ProjectAnnotation.objects.filter(project_id=project_id)
        .order_by("-updated_at")
        .values("updated_at", "id")
        .first()
    )
    if row is None:
        return "empty"
    return f"{row['id']}:{row['updated_at'].isoformat()}"


def _active_proposal_signature_for_owner(project_id: int, owner_id: int) -> dict[str, Any] | None:
    from longdoc.proposal_service import get_active_change_proposal
    from projects.models import Project

    project = Project.objects.filter(id=project_id, owner_id=owner_id).first()
    if project is None:
        return None
    proposal = get_active_change_proposal(project)
    if proposal is None:
        return {"id": 0, "status": "", "updated_at": ""}
    return {
        "id": int(proposal.id),
        "status": str(proposal.status or ""),
        "updated_at": proposal.updated_at.isoformat() if proposal.updated_at else "",
    }


async def sse_project_updates(scope: dict[str, Any], receive, send) -> None:
    path = str(scope.get("path", ""))
    match = SSE_PROJECT_UPDATES_RE.match(path)
    if not match:
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b"Not Found"})
        return

    try:
        project_id = int(match.group("project_id"))
    except (TypeError, ValueError):
        await send({"type": "http.response.start", "status": 400, "headers": []})
        await send({"type": "http.response.body", "body": b"Bad Request"})
        return

    user_id = await sync_to_async(_session_user_id_from_cookie)(scope)
    if not user_id:
        await send({"type": "http.response.start", "status": 401, "headers": []})
        await send({"type": "http.response.body", "body": b"Unauthorized"})
        return

    latest_project = await sync_to_async(_latest_project_version_for_owner)(project_id, user_id)
    proposal_signature = await sync_to_async(_active_proposal_signature_for_owner)(project_id, user_id)
    compile_signature = await sync_to_async(_compile_signature_for_owner)(project_id, user_id)
    annotation_signature = await sync_to_async(_annotation_signature_for_owner)(project_id, user_id)
    if latest_project is None or proposal_signature is None or compile_signature is None or annotation_signature is None:
        await send({"type": "http.response.start", "status": 403, "headers": []})
        await send({"type": "http.response.body", "body": b"Forbidden"})
        return

    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"content-type", b"text/event-stream; charset=utf-8"),
            (b"cache-control", b"no-store, no-cache"),
            (b"x-accel-buffering", b"no"),
            (b"connection", b"keep-alive"),
            (b"x-content-type-options", b"nosniff"),
        ],
    })

    async def send_event(data: dict) -> None:
        line = "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"
        await send({"type": "http.response.body", "body": line.encode(), "more_body": True})

    # Set client retry interval to 1500ms, then send connected event.
    await send({"type": "http.response.body", "body": b"retry: 1500\n\n", "more_body": True})
    await send_event({
        "type": "connected",
        "project_id": project_id,
        "latest_project_version_id": latest_project["id"],
        "latest_project_version_source": latest_project["source"],
        "active_proposal": proposal_signature,
    })

    last_seen_project = int(latest_project["id"])
    last_seen_proposal = dict(proposal_signature)
    last_seen_compile = dict(compile_signature)
    last_seen_annotation = annotation_signature
    while True:
        try:
            event = await asyncio.wait_for(receive(), timeout=1.5)
            if event["type"] == "http.disconnect":
                break
        except asyncio.TimeoutError:
            pass

        latest_project = await sync_to_async(_latest_project_version_for_owner)(project_id, user_id)
        proposal_signature = await sync_to_async(_active_proposal_signature_for_owner)(project_id, user_id)
        compile_signature = await sync_to_async(_compile_signature_for_owner)(project_id, user_id)
        annotation_signature = await sync_to_async(_annotation_signature_for_owner)(project_id, user_id)
        if latest_project is None or proposal_signature is None or compile_signature is None or annotation_signature is None:
            break
        if compile_signature != last_seen_compile:
            last_seen_compile = dict(compile_signature)
            compile_event = await sync_to_async(_compile_event_for_owner)(project_id, user_id)
            if compile_event is not None:
                await send_event(compile_event)
        if int(latest_project["id"]) > last_seen_project:
            last_seen_project = int(latest_project["id"])
            await send_event({
                "type": "project_updated",
                "project_id": project_id,
                "source": latest_project["source"],
                "version_id": latest_project["id"],
            })
        if proposal_signature != last_seen_proposal:
            last_seen_proposal = dict(proposal_signature)
            await send_event({
                "type": "proposal_updated",
                "project_id": project_id,
                "proposal": proposal_signature,
            })
        if annotation_signature != last_seen_annotation:
            last_seen_annotation = annotation_signature
            await send_event({
                "type": "longdoc_updated",
                "project_id": project_id,
            })

    await send({"type": "http.response.body", "body": b"", "more_body": False})
