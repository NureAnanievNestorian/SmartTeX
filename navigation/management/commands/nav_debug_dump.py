import json

from django.core.management.base import BaseCommand, CommandError

from navigation.models import ProjectNavigationIndex
from projects.models import Project


class Command(BaseCommand):
    help = "Print a compact debug dump of a project's navigation index."

    def add_arguments(self, parser):
        parser.add_argument("project_id", type=int)
        parser.add_argument("--json", action="store_true", help="Output JSON instead of human-readable text.")
        parser.add_argument("--regions", action="store_true", help="Include region cards.")

    def handle(self, *args, **options):
        try:
            project = Project.objects.get(id=options["project_id"])
        except Project.DoesNotExist as exc:
            raise CommandError(f"Project {options['project_id']} not found") from exc

        index = ProjectNavigationIndex.objects.filter(project=project).first()
        if index is None:
            raise CommandError(f"Project {project.id} has no navigation index")

        cards = []
        for card in index.file_cards.all().order_by("filename"):
            item = {
                "filename": card.filename,
                "role": card.role,
                "state": card.state,
                "reachability": card.reachability,
                "line_count": card.line_count,
                "is_stale": card.is_stale,
                "summary": card.summary,
            }
            if options["regions"]:
                item["regions"] = [
                    {
                        "order": r.order,
                        "kind": r.region_kind,
                        "title": r.title,
                        "lines": [r.line_start, r.line_end],
                        "state": r.state,
                        "is_stale": r.is_stale,
                    }
                    for r in card.region_cards.all().order_by("order")
                ]
            cards.append(item)

        payload = {
            "project_id": project.id,
            "index": {
                "status": index.status,
                "schema_version": index.schema_version,
                "entrypoint_file": index.entrypoint_file,
                "last_built_version_number": index.last_built_version_number,
                "coverage": index.coverage,
                "build_error": index.build_error,
            },
            "file_cards": cards,
        }

        if options["json"]:
            self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            self.stdout.write(
                f"Navigation index for project {project.id}: status={index.status} "
                f"entrypoint={index.entrypoint_file} files={len(cards)}"
            )
            if index.build_error:
                self.stdout.write(self.style.WARNING(index.build_error))
            for card in cards:
                self.stdout.write(
                    f"- {card['filename']} role={card['role']} state={card['state']} "
                    f"reach={card['reachability']} stale={card['is_stale']}"
                )
