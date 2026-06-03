from dataclasses import dataclass

from projects.models import Project

from .models import AISession, ChangeProposal


@dataclass
class ProjectLockedError(RuntimeError):
    project: Project
    session: AISession

    def __str__(self) -> str:
        return f"Project {self.project.id} is locked by AI session {self.session.id}"

    def suggestion(self) -> str:
        proposal = getattr(self.session, "change_proposal", None) or get_locking_change_proposal(self.project)
        if proposal is not None and proposal.created_by == ChangeProposal.CreatedBy.MCP:
            return (
                f"Update the active MCP proposal #{proposal.id} instead of starting a direct edit: "
                "call propose_document_change again with continue_existing=true and the revised patch_ops. "
                "Do not use direct project file-write tools while this proposal is active."
            )
        if proposal is not None:
            return f"Ask the user to review/discard proposal #{proposal.id} in the UI before starting another edit."
        return "Wait for the active AI session to finish or ask the user to discard/unlock it before editing the project."


def get_locking_session(project: Project) -> AISession | None:
    sessions = (
        AISession.objects.filter(project=project, status__in=AISession.locking_statuses())
        .select_related("change_proposal")
        .order_by("created_at", "id")
    )
    for session in sessions:
        proposal = getattr(session, "change_proposal", None)
        if (
            proposal is not None
            and proposal.created_by == ChangeProposal.CreatedBy.MCP
            and proposal.status in (
                ChangeProposal.Status.FAILED_VALIDATION,
                ChangeProposal.Status.FAILED_COMPILE,
            )
        ):
            continue
        return session
    return None


def get_locking_change_proposal(project: Project) -> ChangeProposal | None:
    return (
        ChangeProposal.objects.filter(project=project, status__in=ChangeProposal.locking_statuses())
        .order_by("created_at", "id")
        .first()
    )


def is_project_locked(project: Project) -> bool:
    return get_locking_session(project) is not None or get_locking_change_proposal(project) is not None


def assert_not_locked(project: Project) -> None:
    session = get_locking_session(project)
    if session is not None:
        raise ProjectLockedError(project=project, session=session)
    proposal = get_locking_change_proposal(project)
    if proposal is not None:
        session = proposal.internal_session or AISession(
            project=project,
            goal=proposal.goal,
            branch_name="HIDDEN",
            worktree_path="HIDDEN",
            status=AISession.Status.ACTIVE,
            expires_at=proposal.expires_at,
        )
        raise ProjectLockedError(project=project, session=session)
