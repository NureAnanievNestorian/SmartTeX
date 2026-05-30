from django.core.management.base import BaseCommand, CommandError

from navigation.services.index_builder import rebuild_navigation_index
from projects.models import Project


class Command(BaseCommand):
    help = "Clear and rebuild the navigation index for one project."

    def add_arguments(self, parser):
        parser.add_argument("project_id", type=int)
        parser.add_argument("--small-model", action="store_true", help="Run small-model enrichment if enabled.")

    def handle(self, *args, **options):
        try:
            project = Project.objects.get(id=options["project_id"])
        except Project.DoesNotExist as exc:
            raise CommandError(f"Project {options['project_id']} not found") from exc

        summary = rebuild_navigation_index(project, use_small_model=bool(options["small_model"]))
        self.stdout.write(summary.as_text())
        if summary.status == "failed":
            raise CommandError(summary.build_error or "navigation index rebuild failed")
