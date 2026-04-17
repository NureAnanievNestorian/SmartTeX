import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.db import transaction
from django.http import HttpRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from .forms import LoginForm, RegisterForm, ResendVerificationForm
from .models import EmailVerificationToken, MCPToken


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def _google_oauth_enabled() -> bool:
    return bool(
        getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "").strip()
        and getattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    )


def _google_redirect_uri(request: HttpRequest) -> str:
    configured = getattr(settings, "GOOGLE_OAUTH_REDIRECT_URI", "").strip()
    if configured:
        return configured
    return request.build_absolute_uri(reverse("google-auth-callback"))


def _username_from_email(email: str) -> str:
    clean = (email or "").strip().lower()
    return clean[:150]


def _ensure_unique_username(email: str) -> str:
    base = _username_from_email(email)
    candidate = base or f"user_{secrets.token_hex(4)}"
    if not User.objects.filter(username=candidate).exists():
        return candidate

    # Keep a deterministic prefix with a random suffix to satisfy unique username constraint.
    for _ in range(8):
        suffix = secrets.token_hex(3)
        max_base_len = max(1, 150 - len(suffix) - 1)
        candidate = f"{base[:max_base_len]}_{suffix}"
        if not User.objects.filter(username=candidate).exists():
            return candidate

    return f"user_{secrets.token_hex(8)}"[:150]


@require_http_methods(["GET", "POST"])
def login_view(request: HttpRequest):
    if request.user.is_authenticated:
        return redirect("projects:dashboard")

    post_data = request.POST.copy() if request.method == "POST" else None
    if post_data and post_data.get("username"):
        post_data["username"] = _resolve_username(post_data.get("username", ""))

    form = LoginForm(request, data=post_data)
    if request.method == "POST":
        lookup_username = post_data.get("username", "") if post_data else ""
        user = User.objects.filter(username=lookup_username).only("id", "is_active").first()
        if user and not user.is_active:
            form.add_error(None, "Пошта не підтверджена. Перевірте лист або запросіть новий лист підтвердження.")
        elif form.is_valid():
            login(request, form.get_user())
            return redirect("projects:dashboard")

    return render(
        request,
        "accounts/login.html",
        {
            "form": form,
            "google_oauth_enabled": _google_oauth_enabled(),
        },
    )


@require_http_methods(["GET", "POST"])
def register_view(request: HttpRequest):
    if request.user.is_authenticated:
        return redirect("projects:dashboard")

    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = form.save()
            user.is_active = False
            user.save(update_fields=["is_active"])
            remaining = _verification_email_cooldown_remaining(user)
            if remaining > 0:
                messages.error(request, f"Лист вже надіслано. Спробуйте через {remaining} с.")
                return render(request, "accounts/register.html", {"form": form})
            token_obj = _issue_email_verification_token(user)
        try:
            _send_verification_email(request, user, token_obj.token)
        except Exception:
            messages.error(request, "Не вдалося надіслати лист підтвердження. Спробуйте ще раз пізніше.")
            return redirect("resend-verification")
        messages.success(request, "Акаунт створено. Перевірте пошту для підтвердження.")
        return redirect("login")

    return render(
        request,
        "accounts/register.html",
        {
            "form": form,
            "google_oauth_enabled": _google_oauth_enabled(),
        },
    )


@require_GET
def google_auth_login_view(request: HttpRequest):
    if request.user.is_authenticated:
        return redirect("projects:dashboard")
    if not _google_oauth_enabled():
        messages.error(request, "Google авторизацію не налаштовано на сервері.")
        return redirect("login")

    state = secrets.token_urlsafe(32)
    request.session["google_oauth_state"] = state
    params = {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": _google_redirect_uri(request),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    return redirect(f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}")


@require_GET
def google_auth_callback_view(request: HttpRequest):
    if not _google_oauth_enabled():
        messages.error(request, "Google авторизацію не налаштовано на сервері.")
        return redirect("login")

    req_state = str(request.GET.get("state", "")).strip()
    session_state = str(request.session.pop("google_oauth_state", "")).strip()
    if not req_state or req_state != session_state:
        messages.error(request, "Невалідний стан OAuth. Спробуйте ще раз.")
        return redirect("login")

    if request.GET.get("error"):
        messages.error(request, "Google авторизацію скасовано.")
        return redirect("login")

    code = str(request.GET.get("code", "")).strip()
    if not code:
        messages.error(request, "Не вдалося отримати код авторизації Google.")
        return redirect("login")

    token_payload = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "redirect_uri": _google_redirect_uri(request),
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    token_req = urllib.request.Request(
        GOOGLE_TOKEN_URL,
        data=token_payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(token_req, timeout=15) as resp:
            token_data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        messages.error(request, "Помилка обміну коду Google OAuth.")
        return redirect("login")

    access_token = str(token_data.get("access_token", "")).strip()
    if not access_token:
        messages.error(request, "Google не повернув access token.")
        return redirect("login")

    userinfo_req = urllib.request.Request(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(userinfo_req, timeout=15) as resp:
            userinfo = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        messages.error(request, "Не вдалося отримати профіль Google.")
        return redirect("login")

    email = str(userinfo.get("email", "")).strip().lower()
    email_verified = bool(userinfo.get("email_verified"))
    if not email or not email_verified:
        messages.error(request, "Google акаунт не має підтвердженої пошти.")
        return redirect("login")

    user = User.objects.filter(email__iexact=email).first()
    if not user:
        username = _ensure_unique_username(email)
        user = User.objects.create_user(
            username=username,
            email=email,
            password=User.objects.make_random_password(),
        )
    updated = False
    if not user.is_active:
        user.is_active = True
        updated = True
    if user.email.lower() != email:
        user.email = email
        updated = True
    if updated:
        user.save(update_fields=["is_active", "email"])

    login(request, user)
    messages.success(request, "Вхід через Google успішний.")
    return redirect("projects:dashboard")


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


def _issue_email_verification_token(user: User) -> EmailVerificationToken:
    EmailVerificationToken.objects.filter(user=user, used_at__isnull=True).delete()
    return EmailVerificationToken.objects.create(
        user=user,
        token=EmailVerificationToken.issue_token(),
        expires_at=EmailVerificationToken.expiry_dt(),
    )


def _verification_email_cooldown_remaining(user: User) -> int:
    cooldown = int(getattr(settings, "EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS", 60))
    if cooldown <= 0:
        return 0
    latest_created = (
        EmailVerificationToken.objects.filter(user=user)
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    if not latest_created:
        return 0
    elapsed = int((timezone.now() - latest_created).total_seconds())
    remaining = cooldown - elapsed
    return remaining if remaining > 0 else 0


def _send_verification_email(request: HttpRequest, user: User, token: str) -> None:
    verify_path = reverse("verify-email", kwargs={"token": token})
    verify_url = request.build_absolute_uri(verify_path)
    subject = "Підтвердження пошти у SmartTeX"
    body = (
        "Вітаємо!\n\n"
        "Щоб завершити реєстрацію, підтвердіть вашу пошту за посиланням:\n"
        f"{verify_url}\n\n"
        "Якщо ви не створювали акаунт у SmartTeX, просто проігноруйте цей лист."
    )
    send_mail(
        subject=subject,
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )


@require_http_methods(["GET", "POST"])
def resend_verification_view(request: HttpRequest):
    if request.user.is_authenticated:
        return redirect("projects:dashboard")

    form = ResendVerificationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = (form.cleaned_data.get("email") or "").strip().lower()
        user = User.objects.filter(email__iexact=email).only("id", "email", "is_active").first()
        if user and not user.is_active:
            remaining = _verification_email_cooldown_remaining(user)
            if remaining > 0:
                messages.error(request, f"Лист вже надіслано. Спробуйте через {remaining} с.")
                return render(request, "accounts/resend_verification.html", {"form": form})
            token_obj = _issue_email_verification_token(user)
            try:
                _send_verification_email(request, user, token_obj.token)
            except Exception:
                messages.error(request, "Не вдалося надіслати лист. Спробуйте ще раз пізніше.")
                return render(request, "accounts/resend_verification.html", {"form": form})
        messages.success(request, "Якщо пошта існує і не підтверджена, ми надіслали лист.")
        return redirect("login")

    return render(request, "accounts/resend_verification.html", {"form": form})


@require_GET
def verify_email_view(request: HttpRequest, token: str):
    token_obj = EmailVerificationToken.objects.select_related("user").filter(token=token).first()
    if not token_obj:
        messages.error(request, "Невалідне посилання підтвердження.")
        return redirect("login")
    if token_obj.used_at is not None:
        messages.info(request, "Це посилання вже використано. Спробуйте увійти.")
        return redirect("login")
    if token_obj.is_expired():
        messages.error(request, "Термін дії посилання завершився. Запросіть новий лист підтвердження.")
        return redirect("resend-verification")

    user = token_obj.user
    token_obj.used_at = timezone.now()
    token_obj.save(update_fields=["used_at"])
    if not user.is_active:
        user.is_active = True
        user.save(update_fields=["is_active"])
    login(request, user)
    messages.success(request, "Пошту підтверджено. Ви увійшли в акаунт.")
    return redirect("projects:dashboard")


@require_http_methods(["POST"])
def api_register(request: HttpRequest) -> JsonResponse:
    data = _json_body(request)
    email = data.get("email", "").strip()
    password = data.get("password", "")

    if not email or not password:
        return JsonResponse({"detail": "email and password are required"}, status=400)

    form = RegisterForm({"email": email, "password1": password, "password2": password})
    if not form.is_valid():
        return JsonResponse({"detail": form.errors}, status=400)

    with transaction.atomic():
        user = form.save()
        user.is_active = False
        user.save(update_fields=["is_active"])
        remaining = _verification_email_cooldown_remaining(user)
        if remaining > 0:
            return JsonResponse(
                {"detail": f"Please retry after {remaining} seconds", "retry_after_seconds": remaining},
                status=429,
            )
        token_obj = _issue_email_verification_token(user)
    try:
        _send_verification_email(request, user, token_obj.token)
    except Exception:
        return JsonResponse({"detail": "Failed to send verification email"}, status=502)
    return JsonResponse({"id": user.id, "email": user.email, "verification_sent": True}, status=201)


@require_http_methods(["POST"])
def api_login(request: HttpRequest) -> JsonResponse:
    data = _json_body(request)
    raw_email = data.get("email", "").strip()
    username = _resolve_username(raw_email)
    password = data.get("password", "")
    if not raw_email or not password:
        return JsonResponse({"detail": "email and password are required"}, status=400)
    existing_user = User.objects.filter(username=username).only("id", "is_active").first()
    if existing_user and not existing_user.is_active:
        return JsonResponse({"detail": "Email is not verified"}, status=403)

    user = authenticate(request, username=username, password=password)
    if user is None:
        return JsonResponse({"detail": "Invalid credentials"}, status=400)

    login(request, user)
    return JsonResponse({"id": user.id, "email": user.email})


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
