from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from mem0_sidecar.core.memory_ops import MemoryService
from mem0_sidecar.http_adapter.dependencies import get_mem0_client, get_session
from mem0_sidecar.http_adapter.project_scope import (
    ensure_project,
    normalized_payload_for_project,
    resolve_project_id,
)

memory_router = APIRouter()


@memory_router.post("/v3/memories/add/")
@memory_router.post("/v3/memories/add", include_in_schema=False)
async def add_memory(
    payload: dict[str, Any],
    request: Request,
    session: Session = Depends(get_session),
    mem0: Any = Depends(get_mem0_client),
) -> dict[str, Any]:
    try:
        project_id = resolve_project_id(request, payload)
        ensure_project(session, request.app.state.settings, project_id)
        session.commit()
        service = MemoryService(session=session, mem0=mem0)
        result = await service.add_memory(
            project_id=project_id,
            payload=normalized_payload_for_project(request, payload),
        )
        session.commit()
        return result
    except Exception:
        session.commit()
        raise


@memory_router.post("/v3/memories/search/")
@memory_router.post("/v3/memories/search", include_in_schema=False)
async def search_memories(
    payload: dict[str, Any],
    request: Request,
    session: Session = Depends(get_session),
    mem0: Any = Depends(get_mem0_client),
) -> dict[str, Any]:
    project_id = resolve_project_id(request, payload)
    ensure_project(session, request.app.state.settings, project_id)
    session.commit()
    service = MemoryService(session=session, mem0=mem0)
    return await service.search_memories(
        project_id=project_id,
        payload=normalized_payload_for_project(request, payload),
    )


@memory_router.get("/v1/memories/{memory_id}/")
@memory_router.get("/v1/memories/{memory_id}", include_in_schema=False)
async def get_memory(
    memory_id: str,
    request: Request,
    session: Session = Depends(get_session),
    mem0: Any = Depends(get_mem0_client),
) -> dict[str, Any]:
    try:
        project_id = resolve_project_id(request)
        ensure_project(session, request.app.state.settings, project_id)
        session.commit()
        service = MemoryService(session=session, mem0=mem0)
        return await service.get_memory(
            project_id=project_id,
            memory_id=memory_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Memory not found") from exc


@memory_router.delete("/v1/memories/{memory_id}/")
@memory_router.delete("/v1/memories/{memory_id}", include_in_schema=False)
async def delete_memory(
    memory_id: str,
    request: Request,
    session: Session = Depends(get_session),
    mem0: Any = Depends(get_mem0_client),
) -> dict[str, Any]:
    try:
        project_id = resolve_project_id(request)
        ensure_project(session, request.app.state.settings, project_id)
        session.commit()
        service = MemoryService(session=session, mem0=mem0)
        result = await service.delete_memory(
            project_id=project_id,
            memory_id=memory_id,
        )
        session.commit()
        return result
    except KeyError as exc:
        session.commit()
        raise HTTPException(status_code=404, detail="Memory not found") from exc
    except Exception:
        session.commit()
        raise
