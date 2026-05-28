from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from small_model.models import UserSmallModelAccess, UserSmallModelQuota, next_month_utc, next_utc_midnight


@dataclass(slots=True)
class QuotaCheckResult:
    quota_ok: bool
    reason: str | None = None
    requests_remaining_today: int = 0
    tokens_remaining_today: int = 0


class SmallModelQuotaService:
    @staticmethod
    def check_quota(user) -> QuotaCheckResult:
        access = UserSmallModelAccess.objects.filter(user=user, enabled=True).first()
        quota = UserSmallModelQuota.objects.filter(user=user).first()
        if access is None or quota is None:
            return QuotaCheckResult(False, "small_model_access_disabled")
        SmallModelQuotaService._reset_if_due(quota)
        if quota.daily_requests_used >= quota.daily_request_limit:
            return QuotaCheckResult(False, "daily_request_limit_exceeded")
        if quota.monthly_requests_used >= quota.monthly_request_limit:
            return QuotaCheckResult(False, "monthly_request_limit_exceeded")
        if quota.daily_tokens_used >= quota.daily_token_limit:
            return QuotaCheckResult(False, "daily_token_limit_exceeded")
        if quota.monthly_tokens_used >= quota.monthly_token_limit:
            return QuotaCheckResult(False, "monthly_token_limit_exceeded")
        return QuotaCheckResult(
            True,
            None,
            max(0, quota.daily_request_limit - quota.daily_requests_used),
            max(0, quota.daily_token_limit - quota.daily_tokens_used),
        )

    @staticmethod
    def reserve_request(user) -> bool:
        with transaction.atomic():
            quota = UserSmallModelQuota.objects.select_for_update().filter(user=user).first()
            if quota is None:
                return False
            SmallModelQuotaService._reset_if_due(quota)
            if (
                quota.daily_requests_used >= quota.daily_request_limit
                or quota.monthly_requests_used >= quota.monthly_request_limit
            ):
                return False
            UserSmallModelQuota.objects.filter(pk=quota.pk).update(
                daily_requests_used=F("daily_requests_used") + 1,
                monthly_requests_used=F("monthly_requests_used") + 1,
            )
            return True

    @staticmethod
    def consume_tokens(user, input_tokens: int, output_tokens: int) -> None:
        total = max(0, int(input_tokens or 0)) + max(0, int(output_tokens or 0))
        if total <= 0:
            return
        UserSmallModelQuota.objects.filter(user=user).update(
            daily_tokens_used=F("daily_tokens_used") + total,
            monthly_tokens_used=F("monthly_tokens_used") + total,
        )

    @staticmethod
    def release_request(user) -> None:
        UserSmallModelQuota.objects.filter(user=user, daily_requests_used__gt=0, monthly_requests_used__gt=0).update(
            daily_requests_used=F("daily_requests_used") - 1,
            monthly_requests_used=F("monthly_requests_used") - 1,
        )

    @staticmethod
    def reset_daily_quota(user) -> None:
        UserSmallModelQuota.objects.filter(user=user).update(
            daily_requests_used=0,
            daily_tokens_used=0,
            daily_reset_at=next_utc_midnight(),
        )

    @staticmethod
    def reset_monthly_quota(user) -> None:
        UserSmallModelQuota.objects.filter(user=user).update(
            monthly_requests_used=0,
            monthly_tokens_used=0,
            monthly_reset_at=next_month_utc(),
        )

    @staticmethod
    def _reset_if_due(quota: UserSmallModelQuota) -> None:
        now = timezone.now()
        fields: list[str] = []
        if quota.daily_reset_at <= now:
            quota.daily_requests_used = 0
            quota.daily_tokens_used = 0
            quota.daily_reset_at = next_utc_midnight()
            fields += ["daily_requests_used", "daily_tokens_used", "daily_reset_at"]
        if quota.monthly_reset_at <= now:
            quota.monthly_requests_used = 0
            quota.monthly_tokens_used = 0
            quota.monthly_reset_at = next_month_utc()
            fields += ["monthly_requests_used", "monthly_tokens_used", "monthly_reset_at"]
        if fields:
            quota.save(update_fields=[*fields, "updated_at"])
