from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from mem0_sidecar.core.consolidation_service import ConsolidationService
from mem0_sidecar.http_adapter.dependencies import get_session
from mem0_sidecar.http_adapter.project_scope import ensure_project

consolidation_router = APIRouter()
SessionDependency = Annotated[Session, Depends(get_session)]


def _service(request: Request, session: Session) -> ConsolidationService:
    return ConsolidationService(
        session=session,
        mem0=request.app.state.mem0_client,
        bridge_routing_ready=False,
    )


@consolidation_router.get(
    "/v1/projects/{project_id}/apps/{app_id}/consolidation"
)
def consolidation_status(
    project_id: str,
    app_id: str,
    request: Request,
    session: SessionDependency,
) -> dict[str, object]:
    ensure_project(session, request.app.state.settings, project_id)
    return _service(request, session).get_status(project_id, app_id)


@consolidation_router.get(
    "/v1/projects/{project_id}/apps/{app_id}/consolidation/runs/{run_id}"
)
def consolidation_run(
    project_id: str,
    app_id: str,
    run_id: str,
    request: Request,
    session: SessionDependency,
) -> dict[str, object]:
    ensure_project(session, request.app.state.settings, project_id)
    try:
        return _service(request, session).get_run(project_id, app_id, run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc


@consolidation_router.get(
    "/v1/projects/{project_id}/apps/{app_id}/consolidation/runs/{run_id}/proposals"
)
def consolidation_proposals(
    project_id: str,
    app_id: str,
    run_id: str,
    request: Request,
    session: SessionDependency,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, object]:
    ensure_project(session, request.app.state.settings, project_id)
    try:
        return _service(request, session).list_proposals(
            project_id=project_id,
            app_id=app_id,
            run_id=run_id,
            page=page,
            page_size=page_size,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
