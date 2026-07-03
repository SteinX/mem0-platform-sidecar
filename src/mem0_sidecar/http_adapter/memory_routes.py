from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from mem0_sidecar.core.memory_ops import MemoryService
from mem0_sidecar.http_adapter.dependencies import get_mem0_client, get_session

memory_router = APIRouter()


def _project_id(request: Request, payload: dict[str, Any] | None = None) -> str:
    if payload:
        for key in ("project_id", "app_id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value

    for key in ("project_id", "app_id"):
        value = request.query_params.get(key)
        if value:
            return value

    return request.app.state.settings.default_project_id


@memory_router.post("/v3/memories/add/")
@memory_router.post("/v3/memories/add", include_in_schema=False)
async def add_memory(
    payload: dict[str, Any],
    request: Request,
    session: Session = Depends(get_session),
    mem0: Any = Depends(get_mem0_client),
) -> dict[str, Any]:
    service = MemoryService(session=session, mem0=mem0)
    try:
        result = await service.add_memory(
            project_id=_project_id(request, payload),
            payload=payload,
        )
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise


@memory_router.post("/v3/memories/search/")
@memory_router.post("/v3/memories/search", include_in_schema=False)
async def search_memories(
    payload: dict[str, Any],
    request: Request,
    session: Session = Depends(get_session),
    mem0: Any = Depends(get_mem0_client),
) -> dict[str, Any]:
    service = MemoryService(session=session, mem0=mem0)
    return await service.search_memories(
        project_id=_project_id(request, payload),
        payload=payload,
    )


@memory_router.get("/v1/memories/{memory_id}/")
@memory_router.get("/v1/memories/{memory_id}", include_in_schema=False)
async def get_memory(
    memory_id: str,
    request: Request,
    session: Session = Depends(get_session),
    mem0: Any = Depends(get_mem0_client),
) -> dict[str, Any]:
    service = MemoryService(session=session, mem0=mem0)
    try:
        return await service.get_memory(
            project_id=_project_id(request),
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
    service = MemoryService(session=session, mem0=mem0)
    try:
        result = await service.delete_memory(
            project_id=_project_id(request),
            memory_id=memory_id,
        )
        session.commit()
        return result
    except KeyError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="Memory not found") from exc
    except Exception:
        session.rollback()
        raise
