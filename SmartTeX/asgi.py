"""
ASGI config for SmartTeX project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/asgi/
"""

import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'SmartTeX.settings')

from django.core.asgi import get_asgi_application
from .realtime_sse import sse_project_updates
from .tinymist_preview_ws import tinymist_preview_control_websocket, tinymist_preview_websocket
from .tinymist_ws import tinymist_websocket

django_asgi_app = get_asgi_application()


async def application(scope, receive, send):
    if scope["type"] == "websocket":
        path = str(scope.get("path", ""))
        if path.startswith("/ws/projects/") and path.endswith("/tinymist/"):
            await tinymist_websocket(scope, receive, send)
        elif path.startswith("/ws/projects/") and path.endswith("/typst-preview/control/"):
            await tinymist_preview_control_websocket(scope, receive, send)
        elif path == "/":
            await tinymist_preview_websocket(scope, receive, send)
        else:
            await send({"type": "websocket.close", "code": 1008})
        return
    if scope["type"] == "http" and str(scope.get("path", "")).startswith("/sse/projects/"):
        await sse_project_updates(scope, receive, send)
        return
    await django_asgi_app(scope, receive, send)
