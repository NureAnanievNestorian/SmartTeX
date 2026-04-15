import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


class MCPToken(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mcp_token")
    token = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"MCP token for {self.user_id}"

    @classmethod
    def issue_token(cls) -> str:
        return secrets.token_hex(32)


class OAuthClient(models.Model):
    client_id = models.CharField(max_length=128, unique=True, db_index=True)
    client_name = models.CharField(max_length=255, blank=True)
    redirect_uris = models.JSONField(default=list)
    grant_types = models.JSONField(default=list)
    response_types = models.JSONField(default=list)
    token_endpoint_auth_method = models.CharField(max_length=32, default="none")
    scope = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"OAuthClient({self.client_id})"

    @classmethod
    def issue_client_id(cls) -> str:
        return f"stx_{secrets.token_urlsafe(18)}"


class OAuthAuthorizationCode(models.Model):
    code = models.CharField(max_length=128, unique=True, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="oauth_codes")
    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE, related_name="authorization_codes")
    redirect_uri = models.TextField()
    scope = models.CharField(max_length=500, blank=True, default="")
    code_challenge = models.CharField(max_length=255)
    code_challenge_method = models.CharField(max_length=10, default="S256")
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    def is_active(self) -> bool:
        return self.used_at is None and not self.is_expired()

    @classmethod
    def issue_code(cls) -> str:
        return secrets.token_urlsafe(48)

    @classmethod
    def expiry_dt(cls) -> timezone.datetime:
        return timezone.now() + timedelta(minutes=10)


class OAuthAccessToken(models.Model):
    token = models.CharField(max_length=128, unique=True, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="oauth_access_tokens")
    client = models.ForeignKey(
        OAuthClient,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="access_tokens",
    )
    scope = models.CharField(max_length=500, blank=True, default="")
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @classmethod
    def issue_token(cls) -> str:
        return secrets.token_urlsafe(48)
