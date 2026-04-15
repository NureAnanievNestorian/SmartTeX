"""
ASGI config for SmartTeX project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/asgi/
"""

import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'SmartTeX.settings')

from django.core.asgi import get_asgi_application
from .realtime_ws import websocket_project_updates

django_asgi_app = get_asgi_application()


async def application(scope, receive, send):
    if scope["type"] == "websocket":
        await websocket_project_updates(scope, receive, send)
        return
    await django_asgi_app(scope, receive, send)
