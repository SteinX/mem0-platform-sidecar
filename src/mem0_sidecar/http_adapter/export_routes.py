from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from mem0_sidecar.core.exports import ExportService, ExportValidationError
from mem0_sidecar.http_adapter.dependencies import get_mem0_client, get_session
from mem0_sidecar.http_adapter.project_scope import ensure_project, resolve_project_id
from mem0_sidecar.store.repositories import ExportJobRepository, MemoryIndexRepository

export_router = APIRouter()
SessionDependency = Annotated[Session, Depends(get_session)]
Mem0Dependency = Annotated[Any, Depends(get_mem0_client)]


def _service(session: Session, mem0: Any) -> ExportService:
    return ExportService(
        exports=ExportJobRepository(session),
        memories=MemoryIndexRepository(session),
        mem0=mem0,
    )


@export_router.post("/v1/exports")
async def create_export(
    payload: dict[str, Any],
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    project_id = str(payload.get("project_id") or resolve_project_id(request))
    ensure_project(session, request.app.state.settings, project_id)
    try:
        result = await _service(session, mem0).create_export(
            project_id=project_id,
            export_format=str(payload.get("format", "json")),
            filters=dict(payload.get("filters") or {}),
        )
    except ExportValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return result


@export_router.get("/v1/exports")
def list_exports(
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    project_id = resolve_project_id(request)
    return _service(session, mem0).list_exports(project_id)


@export_router.get("/v1/exports/{job_id}")
def get_export(
    job_id: str,
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    project_id = resolve_project_id(request)
    try:
        return _service(session, mem0).get_export(project_id, job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Export not found") from exc


@export_router.get("/v1/exports/{job_id}/download")
def download_export(
    job_id: str,
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    project_id = resolve_project_id(request)
    try:
        return _service(session, mem0).download_export(project_id, job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Export not found") from exc
    except ExportValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
