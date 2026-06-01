"""
ASGI WebSocket proxy for Tinymist web preview.

Tinymist preview pages open a websocket on the same origin root path (`/`).
When the preview is embedded through Django, we proxy that websocket to the
per-project preview sidecar started by `projects.tinymist_preview_service`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from urllib.parse import parse_qs
from typing import Any

from asgiref.sync import sync_to_async
from websockets import connect

from .tinymist_ws import _user_id_from_scope

logger = logging.getLogger(__name__)

_REFERER_RE = re.compile(r"/api/projects/(?P<project_id>\d+)/typst-preview(?:/|\?|$)")


def _header(scope: dict[str, Any], name: bytes) -> str:
    for key, value in scope.get("headers", []):
        if key == name:
            try:
                return value.decode("utf-8", errors="ignore")
            except Exception:
                return ""
    return ""


def _summarize_preview_message(message: Any) -> str:
    if isinstance(message, bytes):
        return f"<binary {len(message)} bytes>"
    text = str(message)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            keys = ",".join(sorted(data.keys())[:8])
            kind = data.get("event") or data.get("type") or data.get("kind") or data.get("method") or "object"
            return f"<json {kind} keys={keys}>"
        if isinstance(data, list):
            return f"<json list len={len(data)}>"
    except Exception:
        pass
    compact = text.replace("\n", " ").strip()
    if len(compact) > 220:
        compact = compact[:220] + "…"
    return compact


def _project_id_from_referer(scope: dict[str, Any]) -> int | None:
    referer = _header(scope, b"referer")
    if not referer:
        return None
    match = _REFERER_RE.search(referer)
    if not match:
        return None
    try:
        return int(match.group("project_id"))
    except (TypeError, ValueError):
        return None


def _project_id_from_query(scope: dict[str, Any]) -> int | None:
    raw = scope.get("query_string", b"")
    if not raw:
        return None
    try:
        params = parse_qs(raw.decode("utf-8", errors="ignore"))
        value = (params.get("preview_project") or [None])[0]
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _preview_theme_from_query(scope: dict[str, Any]) -> str | None:
    raw = scope.get("query_string", b"")
    if not raw:
        return None
    try:
        params = parse_qs(raw.decode("utf-8", errors="ignore"))
        value = (params.get("preview_theme") or [None])[0]
        return str(value) if value is not None else None
    except Exception:
        return None


async def tinymist_preview_websocket(scope: dict[str, Any], receive, send) -> None:
    await receive()  # consume websocket.connect
    user_id = await sync_to_async(_user_id_from_scope)(scope)
    if not user_id:
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.close", "code": 4401})
        return

    project_id = _project_id_from_query(scope) or _project_id_from_referer(scope)
    preview_theme = _preview_theme_from_query(scope)
    try:
        from projects.tinymist_preview_service import (
            get_latest_preview_session_for_user,
            get_or_create_preview_session,
        )

        if project_id:
            session = await get_or_create_preview_session(project_id, user_id, invert_colors=preview_theme)
        else:
            session = await get_latest_preview_session_for_user(user_id)
            if session is None:
                logger.warning(
                    "tinymist preview ws: missing referer project id and no active preview session for user=%s headers=%s",
                    user_id,
                    [(k.decode(errors="ignore"), v.decode(errors="ignore")) for k, v in scope.get("headers", [])],
                )
                await send({"type": "websocket.accept"})
                await send({"type": "websocket.close", "code": 4404})
                return
            project_id = session.project_id
    except PermissionError:
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.close", "code": 4403})
        return
    except Exception as exc:
        logger.error(
            "tinymist preview ws: failed to start preview session project=%s user=%s: %s",
            project_id,
            user_id,
            exc,
        )
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.close", "code": 4500})
        return

    await send({"type": "websocket.accept"})

    upstream_url = f"ws://127.0.0.1:{session.data_plane_port}/"
    try:
        upstream = await _connect_preview_upstream(
            upstream_url,
            f"http://127.0.0.1:{session.port}",
            project_id,
            user_id,
            plane="data",
            preview_theme=preview_theme,
        )
        async with upstream:
            b2u = asyncio.create_task(_browser_to_upstream(receive, upstream, "tinymist preview ws"))
            u2b = asyncio.create_task(_upstream_to_browser(upstream, send, "tinymist preview ws"))
            done, pending = await asyncio.wait([b2u, u2b], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                name = "browser->upstream" if task is b2u else "upstream->browser"
                exc = task.exception()
                logger.info("tinymist preview ws: task completed side=%s exc=%s", name, exc)
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    except Exception as exc:
        logger.error(
            "tinymist preview ws: upstream proxy error project=%s user=%s: %s",
            project_id,
            user_id,
            exc,
        )
    finally:
        logger.info("tinymist preview ws: closing project=%s user=%s", project_id, user_id)
        try:
            await send({"type": "websocket.close", "code": 1000})
        except Exception as exc:
            logger.info("tinymist preview ws: close ignored project=%s user=%s err=%s", project_id, user_id, exc)


async def tinymist_preview_control_websocket(scope: dict[str, Any], receive, send) -> None:
    await receive()  # consume websocket.connect
    user_id = await sync_to_async(_user_id_from_scope)(scope)
    if not user_id:
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.close", "code": 4401})
        return

    project_id = _project_id_from_query(scope) or _project_id_from_referer(scope)
    preview_theme = _preview_theme_from_query(scope)
    try:
        from projects.tinymist_preview_service import (
            get_latest_preview_session_for_user,
            get_or_create_preview_session,
        )

        if project_id:
            session = await get_or_create_preview_session(project_id, user_id, invert_colors=preview_theme)
        else:
            session = await get_latest_preview_session_for_user(user_id)
            if session is None:
                logger.warning(
                    "tinymist preview control ws: missing project id and no active preview session for user=%s",
                    user_id,
                )
                await send({"type": "websocket.accept"})
                await send({"type": "websocket.close", "code": 4404})
                return
            project_id = session.project_id
    except PermissionError:
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.close", "code": 4403})
        return
    except Exception as exc:
        logger.error(
            "tinymist preview control ws: failed to start preview session project=%s user=%s: %s",
            project_id,
            user_id,
            exc,
        )
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.close", "code": 4500})
        return

    await send({"type": "websocket.accept"})

    upstream_url = f"ws://127.0.0.1:{session.control_plane_port}/"
    try:
        upstream = await _connect_preview_upstream(
            upstream_url,
            "vscode-webview://smarttex",
            project_id,
            user_id,
            plane="control",
            preview_theme=preview_theme,
        )
        async with upstream:
            b2u = asyncio.create_task(_browser_to_upstream(receive, upstream, "tinymist preview control ws"))
            u2b = asyncio.create_task(_upstream_to_browser(upstream, send, "tinymist preview control ws"))
            done, pending = await asyncio.wait([b2u, u2b], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                name = "browser->upstream" if task is b2u else "upstream->browser"
                exc = task.exception()
                logger.info("tinymist preview control ws: task completed side=%s exc=%s", name, exc)
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    except Exception as exc:
        logger.error(
            "tinymist preview control ws: upstream proxy error project=%s user=%s: %s",
            project_id,
            user_id,
            exc,
        )
    finally:
        logger.info("tinymist preview control ws: closing project=%s user=%s", project_id, user_id)
        try:
            await send({"type": "websocket.close", "code": 1000})
        except Exception as exc:
            logger.info("tinymist preview control ws: close ignored project=%s user=%s err=%s", project_id, user_id, exc)


async def _connect_preview_upstream(url: str, origin: str, project_id: int, user_id: int, plane: str, preview_theme: str | None = None):
    try:
        return await connect(
            url,
            max_size=None,
            origin=origin,
            ping_interval=None,
            ping_timeout=None,
        )
    except OSError as exc:
        if getattr(exc, "errno", None) != 111:
            raise
        from projects.tinymist_preview_service import restart_preview_session

        logger.warning(
            "tinymist preview %s ws: upstream refused project=%s user=%s; restarting preview session",
            plane,
            project_id,
            user_id,
        )
        session = await restart_preview_session(project_id, user_id, invert_colors=preview_theme)
        retry_url = f"ws://127.0.0.1:{session.control_plane_port if plane == 'control' else session.data_plane_port}/"
        retry_origin = "vscode-webview://smarttex" if plane == "control" else f"http://127.0.0.1:{session.port}"
        return await connect(
            retry_url,
            max_size=None,
            origin=retry_origin,
            ping_interval=None,
            ping_timeout=None,
        )


async def _browser_to_upstream(receive, upstream, prefix: str) -> None:
    while True:
        event = await receive()
        if event["type"] == "websocket.disconnect":
            logger.info("%s: browser disconnect code=%s", prefix, event.get("code"))
            return
        if event["type"] != "websocket.receive":
            logger.info("%s: browser event=%s", prefix, event["type"])
            continue
        if event.get("text") is not None:
            logger.info("%s: browser -> upstream %s", prefix, _summarize_preview_message(event["text"]))
            await upstream.send(event["text"])
        elif event.get("bytes") is not None:
            logger.info("%s: browser -> upstream %s", prefix, _summarize_preview_message(event["bytes"]))
            await upstream.send(event["bytes"])
        else:
            logger.info("%s: browser empty receive=%s", prefix, event)


async def _upstream_to_browser(upstream, send, prefix: str) -> None:
    try:
        async for message in upstream:
            logger.info("%s: upstream -> browser %s", prefix, _summarize_preview_message(message))
            if isinstance(message, bytes):
                await send({"type": "websocket.send", "bytes": message})
            else:
                await send({"type": "websocket.send", "text": message})
    finally:
        logger.info("%s: upstream stream ended", prefix)
