from typing import Optional

from django.contrib.auth.models import User
from django.http import HttpRequest
from django.utils import timezone

from .models import MCPToken, OAuthAccessToken


def get_api_user(request: HttpRequest) -> Optional[User]:
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return user

    raw = request.headers.get("Authorization", "")
    token = ""
    if raw.startswith("Token "):
        token = raw[6:].strip()
        if not token:
            return None
        try:
            return MCPToken.objects.select_related("user").get(token=token).user
        except MCPToken.DoesNotExist:
            return None
    if raw.startswith("Bearer "):
        token = raw[7:].strip()
        if not token:
            return None
        access = (
            OAuthAccessToken.objects.select_related("user")
            .filter(token=token, expires_at__gt=timezone.now())
            .first()
        )
        return access.user if access else None
    elif request.headers.get("X-API-Token"):
        token = request.headers["X-API-Token"].strip()
        if not token:
            return None
        try:
            return MCPToken.objects.select_related("user").get(token=token).user
        except MCPToken.DoesNotExist:
            return None
    return None
