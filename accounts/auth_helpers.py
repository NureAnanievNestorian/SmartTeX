from typing import Optional

from django.contrib.auth.models import User
from django.http import HttpRequest
from django.utils import timezone

from .models import MCPToken, OAuthAccessToken


def _resolve_mcp_token_user(token: str) -> Optional[User]:
    try:
        return MCPToken.objects.select_related("user").get(token=token).user
    except MCPToken.DoesNotExist:
        return None


def _resolve_oauth_access_user(token: str) -> Optional[User]:
    access = (
        OAuthAccessToken.objects.select_related("user")
        .filter(token=token, expires_at__gt=timezone.now())
        .first()
    )
    if access:
        return access.user
    return None


def get_api_user(request: HttpRequest) -> Optional[User]:
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return user

    raw = (request.headers.get("Authorization", "") or "").strip()
    lowered = raw.lower()
    if lowered.startswith("token "):
        token = raw[6:].strip()
        if not token:
            return None
        return _resolve_mcp_token_user(token)

    if lowered.startswith("bearer "):
        token = raw[7:].strip()
        if not token:
            return None
        access_user = _resolve_oauth_access_user(token)
        if access_user:
            return access_user
        # Backward-compatible fallback: allow MCP tokens passed as Bearer.
        return _resolve_mcp_token_user(token)

    token = (request.headers.get("X-API-Token", "") or "").strip()
    if token:
        return _resolve_mcp_token_user(token)

    return None
