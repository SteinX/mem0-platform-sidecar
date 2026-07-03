from sqlalchemy.orm import Session

from mem0_sidecar.store.models import Project
from mem0_sidecar.store.repositories import ProjectRepository


def bootstrap_project(
    session: Session,
    *,
    project_id: str,
    name: str,
    mem0_base_url: str,
    default_user_id: str | None = None,
    default_agent_id: str | None = None,
) -> Project:
    return ProjectRepository(session).upsert_default_project(
        project_id=project_id,
        name=name,
        mem0_base_url=mem0_base_url,
        default_user_id=default_user_id,
        default_agent_id=default_agent_id,
    )
