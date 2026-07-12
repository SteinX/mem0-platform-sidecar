from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from mem0_sidecar.core.explorer_filters import (
    MEMORY_FILTER_FIELDS,
    parse_explorer_query,
)
from mem0_sidecar.core.memory_ops import MemoryService
from mem0_sidecar.http_adapter.dependencies import get_mem0_client, get_session
from mem0_sidecar.http_adapter.project_scope import (
    ensure_project,
    normalized_payload_for_project,
    resolve_app_id,
    resolve_project_id,
)

memory_router = APIRouter()
SessionDependency = Annotated[Session, Depends(get_session)]
Mem0Dependency = Annotated[Any, Depends(get_mem0_client)]


@memory_router.post("/v3/memories/add/")
@memory_router.post("/v3/memories/add", include_in_schema=False)
async def add_memory(
    payload: dict[str, Any],
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    project_id = resolve_project_id(request, payload)
    ensure_project(
        session,
        request.app.state.settings,
        project_id,
        default_app_id=resolve_app_id(request, payload),
    )
    session.commit()
    service = MemoryService(session=session, mem0=mem0)
    try:
        result = await service.add_memory(
            project_id=project_id,
            payload=normalized_payload_for_project(request, payload),
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
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    project_id = resolve_project_id(request, payload)
    service = MemoryService(session=session, mem0=mem0)
    return await service.search_memories(
        project_id=project_id,
        payload=normalized_payload_for_project(request, payload),
    )


@memory_router.post("/v1/memories/query")
async def query_memories(
    payload: dict[str, Any],
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    project_id = resolve_project_id(request, payload)
    app_id = resolve_app_id(request, payload) or project_id
    service = MemoryService(session=session, mem0=mem0)
    try:
        query = parse_explorer_query(payload, allowed_fields=MEMORY_FILTER_FIELDS)
        result = await service.query_memories(
            project_id=project_id,
            app_id=app_id,
            query=query,
        )
        session.commit()
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        session.rollback()
        raise

    return {
        "results": result["results"],
        "page": result["page"],
        "page_size": result["page_size"],
        "total": result["total"],
        "has_more": result["page"] * result["page_size"] < result["total"],
        "stale_skipped": result["stale_skipped"],
    }


@memory_router.get("/v1/memories/{memory_id}/")
@memory_router.get("/v1/memories/{memory_id}", include_in_schema=False)
async def get_memory(
    memory_id: str,
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    project_id = resolve_project_id(request)
    request_app_id = resolve_app_id(request)
    service = MemoryService(session=session, mem0=mem0)
    try:
        return await service.get_memory(
            project_id=project_id,
            memory_id=memory_id,
            request_app_id=request_app_id,
        )
    except KeyError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="Memory not found") from exc


@memory_router.patch("/v1/memories/{memory_id}/")
@memory_router.patch("/v1/memories/{memory_id}", include_in_schema=False)
async def update_memory(
    memory_id: str,
    payload: dict[str, Any],
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    project_id = resolve_project_id(request, payload)
    request_app_id = resolve_app_id(request, payload)
    patch = normalized_payload_for_project(request, payload)
    patch.pop("app_id", None)
    service = MemoryService(session=session, mem0=mem0)
    try:
        result = await service.update_memory(
            project_id=project_id,
            memory_id=memory_id,
            request_app_id=request_app_id,
            payload=patch,
        )
        session.commit()
        return result
    except KeyError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="Memory not found") from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        session.rollback()
        raise


@memory_router.get("/v1/memories/{memory_id}/history")
async def get_memory_history(
    memory_id: str,
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    project_id = resolve_project_id(request)
    request_app_id = resolve_app_id(request)
    service = MemoryService(session=session, mem0=mem0)
    try:
        return await service.get_memory_history(
            project_id=project_id,
            memory_id=memory_id,
            request_app_id=request_app_id,
        )
    except KeyError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="Memory not found") from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        session.rollback()
        raise


@memory_router.post("/v1/projects/{path_project_id}/memories/reconcile")
async def reconcile_memories(
    path_project_id: str,
    payload: dict[str, Any],
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, int]:
    project_id = resolve_project_id(request, payload)
    if project_id != path_project_id:
        session.rollback()
        raise HTTPException(status_code=403, detail="Project scope mismatch")

    app_id = resolve_app_id(request, payload) or project_id
    adopt_unscoped = payload.get("adopt_unscoped", False)
    service = MemoryService(session=session, mem0=mem0)
    try:
        if not isinstance(adopt_unscoped, bool):
            raise ValueError("adopt_unscoped must be a boolean")
        result = await service.reconcile_memories(
            project_id=project_id,
            app_id=app_id,
            adopt_unscoped=adopt_unscoped,
            allow_adopt_unscoped=(
                request.app.state.settings.allow_adopt_unscoped_memories
            ),
            default_project_id=request.app.state.settings.default_project_id,
        )
        session.commit()
        return result
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        session.rollback()
        raise


@memory_router.delete("/v1/memories/{memory_id}/")
@memory_router.delete("/v1/memories/{memory_id}", include_in_schema=False)
async def delete_memory(
    memory_id: str,
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    project_id = resolve_project_id(request)
    request_app_id = resolve_app_id(request)
    ensure_project(
        session,
        request.app.state.settings,
        project_id,
        default_app_id=request_app_id,
    )
    session.commit()
    service = MemoryService(session=session, mem0=mem0)
    try:
        result = await service.delete_memory(
            project_id=project_id,
            memory_id=memory_id,
            request_app_id=request_app_id,
        )
        session.commit()
        return result
    except KeyError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="Memory not found") from exc
    except Exception:
        session.rollback()
        raise
