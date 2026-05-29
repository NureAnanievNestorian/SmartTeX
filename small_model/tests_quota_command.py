from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from small_model.models import UserSmallModelQuota


class ResetSmallModelQuotasCommandTests(TestCase):
    def test_command_resets_credits(self) -> None:
        user = User.objects.create_user(username="quota-reset-user", password="secret")
        quota = UserSmallModelQuota.objects.create(user=user, credits_used=Decimal("0.5"))

        call_command("reset_small_model_quotas")
        quota.refresh_from_db()

        self.assertEqual(quota.credits_used, Decimal("0"))
