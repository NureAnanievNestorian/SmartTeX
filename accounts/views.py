import json

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import HttpRequest, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from .forms import LoginForm, RegisterForm
from .models import MCPToken


@require_http_methods(["GET", "POST"])
def login_view(request: HttpRequest):
    if request.user.is_authenticated:
        return redirect("projects:dashboard")

    post_data = request.POST.copy() if request.method == "POST" else None
    if post_data and post_data.get("username"):
        post_data["username"] = _resolve_username(post_data.get("username", ""))

    form = LoginForm(request, data=post_data)
    if request.method == "POST" and form.is_valid():
        login(request, form.get_user())
        return redirect("projects:dashboard")

    return render(request, "accounts/login.html", {"form": form})


@require_http_methods(["GET", "POST"])
def register_view(request: HttpRequest):
    if request.user.is_authenticated:
        return redirect("projects:dashboard")

    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        messages.success(request, "Акаунт створено")
        return redirect("projects:dashboard")

    return render(request, "accounts/register.html", {"form": form})


@login_required
def logout_view(request: HttpRequest):
    logout(request)
    return redirect("login")


def _json_body(request: HttpRequest) -> dict:
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


def _resolve_username(value: str) -> str:
    value = value.strip()
    if "@" not in value:
        return value
    user = User.objects.filter(email__iexact=value).only("username").first()
    return user.username if user else value


@require_http_methods(["POST"])
def api_register(request: HttpRequest) -> JsonResponse:
    data = _json_body(request)
    username = data.get("username", "").strip()
    email = data.get("email", "").strip()
    password = data.get("password", "")

    if not username or not password:
        return JsonResponse({"detail": "username and password are required"}, status=400)

    form = RegisterForm({"username": username, "email": email, "password1": password, "password2": password})
    if not form.is_valid():
        return JsonResponse({"detail": form.errors}, status=400)

    user = form.save()
    login(request, user)
    return JsonResponse({"id": user.id, "username": user.username, "email": user.email}, status=201)


@require_http_methods(["POST"])
def api_login(request: HttpRequest) -> JsonResponse:
    data = _json_body(request)
    raw_username = data.get("username", "").strip()
    username = _resolve_username(raw_username)
    password = data.get("password", "")

    user = authenticate(request, username=username, password=password)
    if user is None:
        return JsonResponse({"detail": "Invalid credentials"}, status=400)

    login(request, user)
    return JsonResponse({"id": user.id, "username": user.username, "email": user.email})


@require_http_methods(["POST"])
def api_logout(request: HttpRequest) -> JsonResponse:
    if request.user.is_authenticated:
        logout(request)
    return JsonResponse({"detail": "ok"})


@login_required
@require_GET
def api_mcp_token(request: HttpRequest) -> JsonResponse:
    mcp_token, _ = MCPToken.objects.get_or_create(
        user=request.user,
        defaults={"token": MCPToken.issue_token()},
    )
    return JsonResponse({"token": mcp_token.token, "created_at": mcp_token.created_at.isoformat()})
