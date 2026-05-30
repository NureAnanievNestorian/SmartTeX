from django.core.management.base import BaseCommand, CommandError

from navigation.services.index_builder import clear_navigation_index
from projects.models import Project


class Command(BaseCommand):
    help = "Delete the navigation index for one project."

    def add_arguments(self, parser):
        parser.add_argument("project_id", type=int)

    def handle(self, *args, **options):
        try:
            project = Project.objects.get(id=options["project_id"])
        except Project.DoesNotExist as exc:
            raise CommandError(f"Project {options['project_id']} not found") from exc
        clear_navigation_index(project)
        self.stdout.write(self.style.SUCCESS(f"Cleared navigation index for project {project.id}"))
