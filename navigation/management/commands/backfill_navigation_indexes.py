from django.core.management.base import BaseCommand

from navigation.models import ProjectNavigationIndex
from navigation.services.index_builder import build_navigation_index
from projects.models import Project


class Command(BaseCommand):
    help = "Build missing navigation indexes for projects. Safe to resume."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--small-model", action="store_true", help="Run small-model enrichment if enabled.")
        parser.add_argument("--force", action="store_true", help="Rebuild even when an index exists.")

    def handle(self, *args, **options):
        qs = Project.objects.all().order_by("id")
        if not options["force"]:
            existing = ProjectNavigationIndex.objects.values_list("project_id", flat=True)
            qs = qs.exclude(id__in=existing)
        if options["limit"]:
            qs = qs[: options["limit"]]

        count = 0
        for project in qs:
            summary = build_navigation_index(
                project,
                use_small_model=bool(options["small_model"]),
                force=bool(options["force"]),
            )
            count += 1
            self.stdout.write(summary.as_text())
        self.stdout.write(self.style.SUCCESS(f"Processed {count} project(s)."))
