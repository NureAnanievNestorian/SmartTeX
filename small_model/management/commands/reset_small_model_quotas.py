from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand

from small_model.models import UserSmallModelQuota


class Command(BaseCommand):
    help = "Reset all user AI credit counters to zero."

    def handle(self, *args, **options):
        count = UserSmallModelQuota.objects.filter(credits_used__gt=0).update(credits_used=Decimal("0"))
        self.stdout.write(self.style.SUCCESS(f"Reset AI credits for {count} user(s)."))
