from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.urls import resolve, Resolver404
from django.utils.cache import patch_vary_headers

from .email_verification import is_user_email_verified


class OAuthCorsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.cors_paths = {
            str(path).strip()
            for path in getattr(settings, "OAUTH_CORS_PATHS", [])
            if str(path).strip()
        }
        self.allowed_origins = [
            str(origin).strip()
            for origin in getattr(settings, "OAUTH_CORS_ALLOWED_ORIGINS", [])
            if str(origin).strip()
        ]
        self.cors_path_prefixes = tuple(
            str(prefix).strip()
            for prefix in getattr(settings, "OAUTH_CORS_PATH_PREFIXES", [])
            if str(prefix).strip()
        )
        self.allowed_methods = str(
            getattr(settings, "OAUTH_CORS_ALLOWED_METHODS", "GET,POST,OPTIONS")
        ).strip()
        self.allowed_headers = str(
            getattr(settings, "OAUTH_CORS_ALLOWED_HEADERS", "Authorization,Content-Type")
        ).strip()

    def __call__(self, request):
        path = request.path or ""
        is_cors_path = self._is_cors_path(path)
        if is_cors_path and request.method == "OPTIONS":
            response = HttpResponse(status=204)
            return self._apply_cors_headers(response, request)

        response = self.get_response(request)
        if is_cors_path:
            self._apply_cors_headers(response, request)
        return response

    def _is_cors_path(self, path: str) -> bool:
        if path in self.cors_paths:
            return True
        return bool(self.cors_path_prefixes) and path.startswith(self.cors_path_prefixes)

    def _apply_cors_headers(self, response, request):
        origin = request.headers.get("Origin", "").strip()
        if self.allowed_origins:
            if origin and origin in self.allowed_origins:
                response["Access-Control-Allow-Origin"] = origin
            else:
                response["Access-Control-Allow-Origin"] = self.allowed_origins[0]
        if self.allowed_methods:
            response["Access-Control-Allow-Methods"] = self.allowed_methods
        if self.allowed_headers:
            response["Access-Control-Allow-Headers"] = self.allowed_headers
        patch_vary_headers(response, ["Origin"])
        return response


class EmailVerificationRequiredMiddleware:
    ALLOWED_VIEW_NAMES = {
        "logout",
        "verify-email",
        "resend-verification",
        "email-verification-required",
        "password_reset",
        "password_reset_done",
        "password_reset_confirm",
        "password_reset_complete",
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user and user.is_authenticated and not is_user_email_verified(user):
            path = request.path or ""
            if not path.startswith(("/static/", "/media/", "/admin/")):
                is_api = path.startswith("/api/")
                allow_request = False
                try:
                    match = resolve(path)
                    allow_request = (
                        match.view_name in self.ALLOWED_VIEW_NAMES
                        or path.startswith("/auth/google/")
                        or path.startswith("/api/auth/")
                    )
                except Resolver404:
                    allow_request = path.startswith("/verify-email/")

                if not allow_request:
                    if is_api:
                        return JsonResponse({"detail": "Email verification required"}, status=403)
                    return redirect("email-verification-required")

        return self.get_response(request)
