from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from small_model.models import UserSmallModelQuota, next_month_utc, next_utc_midnight


class Command(BaseCommand):
    help = "Reset expired daily and monthly small-model quota counters. Intended for cron."

    def handle(self, *args, **options):
        now = timezone.now()
        daily_rows = UserSmallModelQuota.objects.filter(daily_reset_at__lte=now)
        monthly_rows = UserSmallModelQuota.objects.filter(monthly_reset_at__lte=now)

        daily_count = daily_rows.update(
            daily_requests_used=0,
            daily_tokens_used=0,
            daily_reset_at=next_utc_midnight(),
            updated_at=now,
        )
        monthly_count = monthly_rows.update(
            monthly_requests_used=0,
            monthly_tokens_used=0,
            monthly_reset_at=next_month_utc(),
            updated_at=now,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Reset small-model quotas: daily={daily_count}, monthly={monthly_count}"
            )
        )
