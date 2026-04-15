import base64
import hashlib
import json
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from .models import OAuthAccessToken, OAuthAuthorizationCode, OAuthClient


def _json_body(request: HttpRequest) -> dict:
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


def _request_value(request: HttpRequest, json_body: dict, key: str, default: str = "") -> str:
    val = request.POST.get(key, None)
    if val is None or str(val).strip() == "":
        val = json_body.get(key, default)
    return str(val or default).strip()


def _client_id_from_basic_auth(request: HttpRequest) -> str:
    header = request.headers.get("Authorization", "").strip()
    if not header.lower().startswith("basic "):
        return ""
    encoded = header[6:].strip()
    if not encoded:
        return ""
    try:
        decoded = base64.b64decode(encoded).decode("utf-8", errors="ignore")
    except Exception:
        return ""
    if ":" not in decoded:
        return ""
    client_id, _secret = decoded.split(":", 1)
    return client_id.strip()


def _issuer_url(request: HttpRequest) -> str:
    configured = getattr(settings, "OAUTH_ISSUER_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    return request.build_absolute_uri("/").rstrip("/")


def _make_url(request: HttpRequest, path: str) -> str:
    issuer = _issuer_url(request)
    return f"{issuer}{path if path.startswith('/') else '/' + path}"


def _pkce_ok(code_verifier: str, code_challenge: str, method: str) -> bool:
    if method == "plain":
        return code_verifier == code_challenge
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
    return encoded == code_challenge


def _redirect_with_params(base_url: str, params: dict[str, str]) -> HttpResponseRedirect:
    joiner = "&" if "?" in base_url else "?"
    return HttpResponseRedirect(f"{base_url}{joiner}{urlencode(params)}")


@require_GET
def oauth_authorization_server_metadata(request: HttpRequest) -> JsonResponse:
    return JsonResponse(
        {
            "issuer": _issuer_url(request),
            "authorization_endpoint": _make_url(request, "/oauth/authorize/"),
            "token_endpoint": _make_url(request, "/oauth/token/"),
            "registration_endpoint": _make_url(request, "/oauth/register/"),
            "introspection_endpoint": _make_url(request, "/oauth/introspect/"),
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256", "plain"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["openid", "profile", "smarttex:read", "smarttex:write"],
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def oauth_register(request: HttpRequest) -> JsonResponse:
    body = _json_body(request)
    redirect_uris = body.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return JsonResponse({"error": "invalid_redirect_uri"}, status=400)
    token_auth_method = str(body.get("token_endpoint_auth_method", "none")).strip() or "none"
    if token_auth_method not in {"none", "client_secret_basic", "client_secret_post"}:
        return JsonResponse({"error": "invalid_client_metadata"}, status=400)

    client_id = OAuthClient.issue_client_id()
    while OAuthClient.objects.filter(client_id=client_id).exists():
        client_id = OAuthClient.issue_client_id()

    client = OAuthClient.objects.create(
        client_id=client_id,
        client_name=str(body.get("client_name", "")).strip(),
        redirect_uris=redirect_uris,
        grant_types=body.get("grant_types") or ["authorization_code"],
        response_types=body.get("response_types") or ["code"],
        token_endpoint_auth_method=token_auth_method,
        scope=str(body.get("scope", "openid profile smarttex:read smarttex:write")).strip(),
    )
    return JsonResponse(
        {
            "client_id": client.client_id,
            "client_name": client.client_name,
            "redirect_uris": client.redirect_uris,
            "grant_types": client.grant_types,
            "response_types": client.response_types,
            "token_endpoint_auth_method": client.token_endpoint_auth_method,
            "scope": client.scope,
            "client_id_issued_at": int(client.created_at.timestamp()),
        },
        status=201,
    )


@login_required
@require_http_methods(["GET", "POST"])
def oauth_authorize(request: HttpRequest):
    params = request.GET if request.method == "GET" else request.POST
    response_type = params.get("response_type", "")
    client_id = params.get("client_id", "").strip()
    redirect_uri = params.get("redirect_uri", "").strip()
    state = params.get("state", "")
    scope = params.get("scope", "openid profile smarttex:read smarttex:write").strip()
    code_challenge = params.get("code_challenge", "").strip()
    code_challenge_method = params.get("code_challenge_method", "S256").strip()

    if response_type != "code":
        return JsonResponse({"error": "unsupported_response_type"}, status=400)
    if not client_id or not redirect_uri or not code_challenge:
        return JsonResponse({"error": "invalid_request"}, status=400)
    if code_challenge_method not in {"S256", "plain"}:
        return JsonResponse({"error": "invalid_request"}, status=400)

    client = get_object_or_404(OAuthClient, client_id=client_id)
    if redirect_uri not in (client.redirect_uris or []):
        return JsonResponse({"error": "invalid_redirect_uri"}, status=400)

    if request.method == "GET":
        return render(
            request,
            "accounts/oauth_authorize.html",
            {
                "client": client,
                "redirect_uri": redirect_uri,
                "scope": scope,
                "state": state,
                "response_type": response_type,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
            },
        )

    if params.get("action") != "approve":
        return _redirect_with_params(
            redirect_uri,
            {"error": "access_denied", "state": state},
        )

    code = OAuthAuthorizationCode.issue_code()
    while OAuthAuthorizationCode.objects.filter(code=code).exists():
        code = OAuthAuthorizationCode.issue_code()

    OAuthAuthorizationCode.objects.create(
        code=code,
        user=request.user,
        client=client,
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        expires_at=OAuthAuthorizationCode.expiry_dt(),
    )
    return _redirect_with_params(
        redirect_uri,
        {"code": code, "state": state},
    )


@csrf_exempt
@require_http_methods(["POST"])
def oauth_token(request: HttpRequest) -> JsonResponse:
    body = _json_body(request)
    grant_type = _request_value(request, body, "grant_type")
    code = _request_value(request, body, "code")
    client_id = _request_value(request, body, "client_id")
    redirect_uri = _request_value(request, body, "redirect_uri")
    code_verifier = _request_value(request, body, "code_verifier")
    if not client_id:
        client_id = _client_id_from_basic_auth(request)

    if grant_type != "authorization_code":
        return JsonResponse({"error": "unsupported_grant_type"}, status=400)
    if not code or not redirect_uri or not code_verifier:
        return JsonResponse({"error": "invalid_request"}, status=400)

    auth_code = (
        OAuthAuthorizationCode.objects.select_related("user", "client")
        .filter(code=code)
        .first()
    )
    if not auth_code or not auth_code.is_active():
        return JsonResponse({"error": "invalid_grant"}, status=400)
    effective_client_id = client_id or auth_code.client.client_id
    if auth_code.client.client_id != effective_client_id or auth_code.redirect_uri != redirect_uri:
        return JsonResponse({"error": "invalid_grant"}, status=400)
    if not _pkce_ok(code_verifier, auth_code.code_challenge, auth_code.code_challenge_method):
        return JsonResponse({"error": "invalid_grant"}, status=400)

    access_token_value = OAuthAccessToken.issue_token()
    while OAuthAccessToken.objects.filter(token=access_token_value).exists():
        access_token_value = OAuthAccessToken.issue_token()

    expires_in = int(getattr(settings, "OAUTH_ACCESS_TOKEN_TTL_SECONDS", 3600))
    expires_at = timezone.now() + timedelta(seconds=expires_in)
    token_obj = OAuthAccessToken.objects.create(
        token=access_token_value,
        user=auth_code.user,
        client=auth_code.client,
        scope=auth_code.scope,
        expires_at=expires_at,
    )
    auth_code.used_at = timezone.now()
    auth_code.save(update_fields=["used_at"])

    return JsonResponse(
        {
            "access_token": token_obj.token,
            "token_type": "Bearer",
            "expires_in": expires_in,
            "scope": token_obj.scope,
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def oauth_introspect(request: HttpRequest) -> JsonResponse:
    secret = str(getattr(settings, "OAUTH_INTROSPECTION_SECRET", "")).strip()
    if secret and request.headers.get("X-Introspection-Secret", "").strip() != secret:
        return JsonResponse({"active": False}, status=401)

    token = request.POST.get("token", "").strip()
    if not token:
        data = _json_body(request)
        token = str(data.get("token", "")).strip()
    if not token:
        return JsonResponse({"active": False})

    obj = (
        OAuthAccessToken.objects.select_related("user", "client")
        .filter(token=token)
        .first()
    )
    if not obj or obj.is_expired():
        return JsonResponse({"active": False})

    return JsonResponse(
        {
            "active": True,
            "client_id": obj.client.client_id if obj.client else "",
            "username": obj.user.username,
            "sub": str(obj.user_id),
            "scope": obj.scope,
            "exp": int(obj.expires_at.timestamp()),
        }
    )
