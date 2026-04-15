from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from accounts.models import MCPToken


class Command(BaseCommand):
    help = "Create or rotate MCP token for a user"

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--rotate", action="store_true")

    def handle(self, *args, **options):
        User = get_user_model()
        username = options["username"]
        rotate = options["rotate"]

        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist as exc:
            raise CommandError(f"User '{username}' not found") from exc

        token_obj, created = MCPToken.objects.get_or_create(
            user=user,
            defaults={"token": MCPToken.issue_token()},
        )

        if rotate:
            token_obj.token = MCPToken.issue_token()
            token_obj.save(update_fields=["token"])

        state = "created" if created else ("rotated" if rotate else "existing")
        self.stdout.write(self.style.SUCCESS(f"{state}: {token_obj.token}"))
