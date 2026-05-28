from django.core.management.base import BaseCommand

from longdoc.session_service import expire_stale_sessions


class Command(BaseCommand):
    help = "Expire AI sessions whose expiry time has passed and clean up their worktrees and branches"

    def handle(self, *args, **options):
        count = expire_stale_sessions()
        if count:
            self.stdout.write(self.style.SUCCESS(f"Expired {count} AI session(s)."))
        else:
            self.stdout.write("No sessions to expire.")
