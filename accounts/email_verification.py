from django.contrib.auth.models import User
from django.utils import timezone

from .models import EmailVerificationState


def is_user_email_verified(user: User) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    state = getattr(user, "email_verification_state", None)
    if state is not None:
        return bool(state.verified_at)
    # Legacy fallback: before dedicated verification state existed.
    return bool(user.is_active)


def ensure_unverified_state(user: User) -> EmailVerificationState:
    state, _ = EmailVerificationState.objects.get_or_create(user=user)
    if state.verified_at is not None:
        state.verified_at = None
        state.save(update_fields=["verified_at", "updated_at"])
    return state


def mark_user_email_verified(user: User) -> EmailVerificationState:
    state, _ = EmailVerificationState.objects.get_or_create(user=user)
    if state.verified_at is None:
        state.verified_at = timezone.now()
        state.save(update_fields=["verified_at", "updated_at"])
    return state
