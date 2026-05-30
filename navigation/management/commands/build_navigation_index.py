from django.core.management.base import BaseCommand, CommandError

from navigation.services.index_builder import build_navigation_index
from projects.models import Project


class Command(BaseCommand):
    help = "Build the navigation index for one project."

    def add_arguments(self, parser):
        parser.add_argument("project_id", type=int)
        parser.add_argument("--small-model", action="store_true", help="Run small-model enrichment if enabled.")
        parser.add_argument("--force", action="store_true", help="Force refresh even when an index exists.")

    def handle(self, *args, **options):
        try:
            project = Project.objects.get(id=options["project_id"])
        except Project.DoesNotExist as exc:
            raise CommandError(f"Project {options['project_id']} not found") from exc

        summary = build_navigation_index(
            project,
            use_small_model=bool(options["small_model"]),
            force=bool(options["force"]),
        )
        self.stdout.write(summary.as_text())
        if summary.status == "failed":
            raise CommandError(summary.build_error or "navigation index build failed")
