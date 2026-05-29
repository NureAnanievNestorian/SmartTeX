from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db.models import F

from small_model.models import SmallModelConfig, UserSmallModelAccess, UserSmallModelQuota


@dataclass(slots=True)
class QuotaCheckResult:
    quota_ok: bool
    reason: str | None = None
    credits_remaining: Decimal = Decimal("0")
    credits_used: Decimal = Decimal("0")
    credits_limit: Decimal = Decimal("0")


class SmallModelQuotaService:
    @staticmethod
    def check_quota(user) -> QuotaCheckResult:
        access = UserSmallModelAccess.objects.filter(user=user, enabled=True).first()
        quota = UserSmallModelQuota.objects.filter(user=user).first()
        if access is None or quota is None:
            return QuotaCheckResult(False, "small_model_access_disabled")
        if quota.credits_used >= quota.credits_limit:
            return QuotaCheckResult(
                False,
                "credits_limit_exceeded",
                credits_remaining=Decimal("0"),
                credits_used=quota.credits_used,
                credits_limit=quota.credits_limit,
            )
        remaining = max(Decimal("0"), quota.credits_limit - quota.credits_used)
        return QuotaCheckResult(
            True,
            None,
            credits_remaining=remaining,
            credits_used=quota.credits_used,
            credits_limit=quota.credits_limit,
        )

    @staticmethod
    def get_cost(provider: str, model_name: str, input_tokens: int, output_tokens: int) -> Decimal:
        cfg = SmallModelConfig.objects.filter(provider=provider, model_name=model_name, is_active=True).first()
        if cfg is None:
            return Decimal("0")
        input_cost = Decimal(max(0, int(input_tokens or 0))) * cfg.input_price_per_million_tokens / Decimal("1000000")
        output_cost = Decimal(max(0, int(output_tokens or 0))) * cfg.output_price_per_million_tokens / Decimal("1000000")
        return input_cost + output_cost

    @staticmethod
    def consume_tokens(user, input_tokens: int, output_tokens: int, provider: str = "", model_name: str = "") -> None:
        cost = SmallModelQuotaService.get_cost(provider, model_name, input_tokens, output_tokens)
        if cost > 0:
            UserSmallModelQuota.objects.filter(user=user).update(
                credits_used=F("credits_used") + cost,
            )

    @staticmethod
    def reset_credits(user) -> None:
        UserSmallModelQuota.objects.filter(user=user).update(credits_used=Decimal("0"))
