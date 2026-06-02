"""
ASGI WebSocket handler for the tinymist LSP bridge.

Path: /ws/projects/<project_id>/tinymist/

Acts as a transparent proxy between the browser and a per-session tinymist
subprocess managed by projects.tinymist_service.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from http.cookies import SimpleCookie
from typing import Any

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import SESSION_KEY

logger = logging.getLogger(__name__)

_PATH_RE = re.compile(r"^/ws/projects/(?P<project_id>\d+)/tinymist/?$")


def _debug_enabled() -> bool:
    return bool(getattr(settings, "TINYMIST_DEBUG", False))


def _user_id_from_scope(scope: dict[str, Any]) -> int | None:
    for key, value in scope.get("headers", []):
        if key != b"cookie":
            continue
        try:
            raw = value.decode("utf-8", errors="ignore")
        except Exception:
            return None
        cookie = SimpleCookie()
        cookie.load(raw)
        morsel = cookie.get(settings.SESSION_COOKIE_NAME)
        if not morsel or not morsel.value:
            return None
        mod = __import__(settings.SESSION_ENGINE, fromlist=["SessionStore"])
        session = mod.SessionStore(session_key=morsel.value)
        uid = session.get(SESSION_KEY)
        try:
            return int(uid) if uid is not None else None
        except (TypeError, ValueError):
            return None
    return None


async def tinymist_websocket(scope: dict[str, Any], receive, send) -> None:
    match = _PATH_RE.match(str(scope.get("path", "")))
    if not match:
        await send({"type": "websocket.close", "code": 1008})
        return

    try:
        project_id = int(match.group("project_id"))
    except (TypeError, ValueError):
        await send({"type": "websocket.close", "code": 1008})
        return

    # Accept the connection before doing anything else
    await receive()  # consume websocket.connect
    await send({"type": "websocket.accept"})

    user_id = await sync_to_async(_user_id_from_scope)(scope)
    if not user_id:
        await _ws_json(send, {"type": "tinymist_error", "code": 4401, "message": "Unauthorized"})
        await send({"type": "websocket.close", "code": 4401})
        return

    try:
        from projects.tinymist_service import get_or_create_session

        session = await get_or_create_session(project_id, user_id)
    except PermissionError:
        await _ws_json(send, {"type": "tinymist_error", "code": 4403, "message": "Forbidden"})
        await send({"type": "websocket.close", "code": 4403})
        return
    except Exception as exc:
        logger.error(
            "tinymist ws: failed to start session project=%s user=%s: %s",
            project_id,
            user_id,
            exc,
        )
        await _ws_json(send, {"type": "tinymist_error", "code": 4500, "message": str(exc)})
        await send({"type": "websocket.close", "code": 4500})
        return

    if _debug_enabled():
        logger.info("tinymist ws: connected project=%s user=%s", project_id, user_id)

    await _ws_json(send, {
        "type": "tinymist_connected",
        "project_id": project_id,
        "root_uri": session.project_root.as_uri(),
        "semantic_tokens_legend": session.semantic_tokens_legend,
    })

    b2l = asyncio.create_task(_browser_to_lsp(receive, session))
    l2b = asyncio.create_task(_lsp_to_browser(session, send))
    _, pending = await asyncio.wait([b2l, l2b], return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        await send({"type": "websocket.close", "code": 1000})
    except Exception:
        pass
    if bool(getattr(settings, "TINYMIST_CLOSE_ON_WS_DISCONNECT", False)):
        try:
            from projects.tinymist_service import close_session

            await close_session(project_id, user_id)
        except Exception:
            logger.exception("tinymist ws: failed to close session project=%s user=%s", project_id, user_id)


async def _ws_json(send, data: dict) -> None:
    await send({"type": "websocket.send", "text": json.dumps(data, ensure_ascii=False)})


async def _browser_to_lsp(receive, session) -> None:
    while True:
        event = await receive()
        if event["type"] == "websocket.disconnect":
            if _debug_enabled():
                logger.info(
                    "tinymist ws: browser disconnected project=%s user=%s",
                    session.project_id,
                    session.user_id,
                )
            return
        if event["type"] != "websocket.receive":
            continue
        text = event.get("text") or ""
        if not text:
            continue
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            continue
        if _debug_enabled():
            logger.info(
                "tinymist ws: browser -> lsp project=%s user=%s method=%s id=%s",
                session.project_id,
                session.user_id,
                msg.get("method"),
                msg.get("id"),
            )
        try:
            await session.send_raw(msg)
        except Exception as exc:
            logger.error("tinymist ws: send_raw error: %s", exc)
            return


async def _lsp_to_browser(session, send) -> None:
    while True:
        try:
            msg = await session.outbox.get()
            if _debug_enabled():
                logger.info(
                    "tinymist ws: lsp -> browser project=%s user=%s method=%s id=%s has_result=%s has_error=%s",
                    session.project_id,
                    session.user_id,
                    msg.get("method"),
                    msg.get("id"),
                    "result" in msg,
                    "error" in msg,
                )
            await _ws_json(send, msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("tinymist ws: lsp_to_browser error: %s", exc)
            return
