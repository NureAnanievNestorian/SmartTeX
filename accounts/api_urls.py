from django.urls import path

from .views import api_login, api_logout, api_register

urlpatterns = [
    path("auth/login/", api_login),
    path("auth/logout/", api_logout),
    path("auth/register/", api_register),
]
