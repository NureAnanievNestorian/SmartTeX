from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import resolve, Resolver404

from .email_verification import is_user_email_verified


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
