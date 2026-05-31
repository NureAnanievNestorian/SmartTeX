from django.apps import AppConfig


class ProjectsConfig(AppConfig):
    name = 'projects'

    def ready(self):
        from .services import schedule_github_sync
        from django.db import OperationalError, ProgrammingError
        try:
            Project = self.get_model("Project")
            qs = Project.objects.filter(github_sync_enabled=True).exclude(github_repo_url="").select_related("owner__github_installation")
            for project in qs:
                schedule_github_sync(project)
        except (OperationalError, ProgrammingError):
            pass
