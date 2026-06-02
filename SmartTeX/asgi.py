"""
ASGI config for SmartTeX project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/asgi/
"""

import asyncio
import contextlib
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'SmartTeX.settings')

from django.conf import settings
from django.core.asgi import get_asgi_application
from .realtime_sse import sse_project_updates
from .tinymist_preview_ws import tinymist_preview_control_websocket, tinymist_preview_websocket
from .tinymist_ws import tinymist_websocket

django_asgi_app = get_asgi_application()
_tinymist_reaper_task = None


async def _tinymist_reaper_loop():
    interval = max(10, int(getattr(settings, "TINYMIST_SESSION_IDLE_TIMEOUT", 60)) // 2)
    while True:
        await asyncio.sleep(interval)
        with contextlib.suppress(Exception):
            from projects.tinymist_service import reap_idle_sessions

            await reap_idle_sessions()
        with contextlib.suppress(Exception):
            from projects.tinymist_preview_service import reap_idle_preview_sessions

            await reap_idle_preview_sessions()


def _ensure_tinymist_reaper_task():
    global _tinymist_reaper_task
    if _tinymist_reaper_task is not None and not _tinymist_reaper_task.done():
        return
    if not (
        bool(getattr(settings, "TINYMIST_LSP_ENABLED", True))
        or bool(getattr(settings, "TINYMIST_PREVIEW_ENABLED", True))
    ):
        return
    _tinymist_reaper_task = asyncio.create_task(_tinymist_reaper_loop())


async def application(scope, receive, send):
    _ensure_tinymist_reaper_task()
    if scope["type"] == "websocket":
        path = str(scope.get("path", ""))
        if path.startswith("/ws/projects/") and path.endswith("/tinymist/"):
            await tinymist_websocket(scope, receive, send)
        elif path.startswith("/ws/projects/") and path.endswith("/typst-preview/control/"):
            await tinymist_preview_control_websocket(scope, receive, send)
        elif path in {"/", "/ws/typst-preview", "/ws/typst-preview/"}:
            await tinymist_preview_websocket(scope, receive, send)
        else:
            await send({"type": "websocket.close", "code": 1008})
        return
    if scope["type"] == "http" and str(scope.get("path", "")).startswith("/sse/projects/"):
        await sse_project_updates(scope, receive, send)
        return
    await django_asgi_app(scope, receive, send)
