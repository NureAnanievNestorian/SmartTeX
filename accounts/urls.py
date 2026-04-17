from django.contrib.auth import views as auth_views
from django.urls import path
from django.urls import reverse_lazy

from .views import (
    google_auth_callback_view,
    google_auth_login_view,
    login_view,
    logout_view,
    register_view,
    resend_verification_view,
    verify_email_view,
)
from .oauth_views import (
    oauth_authorization_server_metadata,
    oauth_authorize,
    oauth_introspect,
    oauth_register,
    oauth_token,
)

urlpatterns = [
    path("login/", login_view, name="login"),
    path("register/", register_view, name="register"),
    path("logout/", logout_view, name="logout"),
    path(
        "password-reset/",
        auth_views.PasswordResetView.as_view(
            template_name="accounts/password_reset_form.html",
            email_template_name="accounts/password_reset_email.txt",
            subject_template_name="accounts/password_reset_subject.txt",
            success_url=reverse_lazy("password_reset_done"),
        ),
        name="password_reset",
    ),
    path(
        "password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="accounts/password_reset_done.html",
        ),
        name="password_reset_done",
    ),
    path(
        "reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="accounts/password_reset_confirm.html",
            success_url=reverse_lazy("password_reset_complete"),
        ),
        name="password_reset_confirm",
    ),
    path(
        "reset/done/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="accounts/password_reset_complete.html",
        ),
        name="password_reset_complete",
    ),
    path("auth/google/login/", google_auth_login_view, name="google-auth-login"),
    path("auth/google/callback/", google_auth_callback_view, name="google-auth-callback"),
    path("verify-email/<str:token>/", verify_email_view, name="verify-email"),
    path("resend-verification/", resend_verification_view, name="resend-verification"),
    path(".well-known/oauth-authorization-server", oauth_authorization_server_metadata),
    path("oauth/authorize/", oauth_authorize),
    path("oauth/register/", oauth_register),
    path("oauth/token/", oauth_token),
    path("oauth/introspect/", oauth_introspect),
]
