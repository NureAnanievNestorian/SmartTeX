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


def _latest_mcp_version_id_for_owner(project_id: int, owner_id: int) -> int | None:
    from projects.models import ProjectVersion

    qs = (
        ProjectVersion.objects.filter(
            project_id=project_id,
            project__owner_id=owner_id,
            source="mcp",
        )
        .order_by("-id")
        .values_list("id", flat=True)
    )
    latest = qs.first()
    return int(latest) if latest is not None else 0


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

    latest = await sync_to_async(_latest_mcp_version_id_for_owner)(project_id, user_id)
    if latest is None:
        await send({"type": "http.response.start", "status": 403, "headers": []})
        await send({"type": "http.response.body", "body": b"Forbidden"})
        return

    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"content-type", b"text/event-stream; charset=utf-8"),
            (b"cache-control", b"no-cache"),
            (b"x-accel-buffering", b"no"),
            (b"connection", b"keep-alive"),
        ],
    })

    async def send_event(data: dict) -> None:
        line = "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"
        await send({"type": "http.response.body", "body": line.encode(), "more_body": True})

    # Set client retry interval to 1500ms, then send connected event.
    await send({"type": "http.response.body", "body": b"retry: 1500\n\n", "more_body": True})
    await send_event({"type": "connected", "project_id": project_id, "latest_mcp_version_id": latest})

    last_seen = latest
    while True:
        try:
            event = await asyncio.wait_for(receive(), timeout=1.5)
            if event["type"] == "http.disconnect":
                break
        except asyncio.TimeoutError:
            pass

        latest = await sync_to_async(_latest_mcp_version_id_for_owner)(project_id, user_id)
        if latest is None:
            break
        if latest > last_seen:
            last_seen = latest
            await send_event({
                "type": "project_updated",
                "project_id": project_id,
                "source": "mcp",
                "version_id": latest,
            })

    await send({"type": "http.response.body", "body": b"", "more_body": False})
