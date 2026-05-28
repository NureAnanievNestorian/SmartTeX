from dataclasses import dataclass

from projects.models import Project

from .models import AISession


@dataclass
class ProjectLockedError(RuntimeError):
    project: Project
    session: AISession

    def __str__(self) -> str:
        return f"Project {self.project.id} is locked by AI session {self.session.id}"


def get_locking_session(project: Project) -> AISession | None:
    return (
        AISession.objects.filter(project=project, status__in=AISession.locking_statuses())
        .order_by("created_at", "id")
        .first()
    )


def is_project_locked(project: Project) -> bool:
    return get_locking_session(project) is not None


def assert_not_locked(project: Project) -> None:
    session = get_locking_session(project)
    if session is not None:
        raise ProjectLockedError(project=project, session=session)
