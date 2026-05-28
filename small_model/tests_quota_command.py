from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from small_model.models import UserSmallModelQuota


class ResetSmallModelQuotasCommandTests(TestCase):
    def test_command_resets_due_daily_and_monthly_counters(self) -> None:
        user = User.objects.create_user(username="quota-reset-user", password="secret")
        quota = UserSmallModelQuota.objects.create(
            user=user,
            daily_requests_used=7,
            monthly_requests_used=25,
            daily_tokens_used=300,
            monthly_tokens_used=1200,
            daily_reset_at=timezone.now() - timedelta(minutes=1),
            monthly_reset_at=timezone.now() - timedelta(minutes=1),
        )

        call_command("reset_small_model_quotas")
        quota.refresh_from_db()

        self.assertEqual(quota.daily_requests_used, 0)
        self.assertEqual(quota.daily_tokens_used, 0)
        self.assertEqual(quota.monthly_requests_used, 0)
        self.assertEqual(quota.monthly_tokens_used, 0)
        self.assertGreater(quota.daily_reset_at, timezone.now())
        self.assertGreater(quota.monthly_reset_at, timezone.now())
