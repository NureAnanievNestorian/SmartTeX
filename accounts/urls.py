from django.urls import path

from .views import login_view, logout_view, register_view
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
    path(".well-known/oauth-authorization-server", oauth_authorization_server_metadata),
    path("oauth/authorize/", oauth_authorize),
    path("oauth/register/", oauth_register),
    path("oauth/token/", oauth_token),
    path("oauth/introspect/", oauth_introspect),
]
