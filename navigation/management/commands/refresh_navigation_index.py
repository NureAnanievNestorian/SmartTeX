from django.core.management.base import BaseCommand, CommandError

from navigation.services.refresh import refresh_navigation_index
from projects.models import Project


class Command(BaseCommand):
    help = "Partially refresh the navigation index for one project."

    def add_arguments(self, parser):
        parser.add_argument("project_id", type=int)
        parser.add_argument("files", nargs="*", help="Optional project-relative files to refresh.")

    def handle(self, *args, **options):
        try:
            project = Project.objects.get(id=options["project_id"])
        except Project.DoesNotExist as exc:
            raise CommandError(f"Project {options['project_id']} not found") from exc
        summary = refresh_navigation_index(project, files=options.get("files") or None)
        self.stdout.write(
            f"status={summary.status} touched={len(summary.touched_files)} "
            f"files_updated={summary.files_updated} files_missing={summary.files_marked_missing} "
            f"regions_updated={summary.regions_updated}"
        )
        if summary.error:
            self.stdout.write(self.style.WARNING(summary.error))
        if summary.status == "failed":
            raise CommandError(summary.error or "navigation index refresh failed")
