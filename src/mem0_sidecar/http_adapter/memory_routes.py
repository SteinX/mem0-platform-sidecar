import re
from typing import Annotated, Any
from urllib.parse import unquote, unquote_to_bytes

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session
from starlette.routing import Match
from starlette.types import Scope

from mem0_sidecar.core.explorer_filters import (
    MEMORY_FILTER_FIELDS,
    parse_explorer_query,
)
from mem0_sidecar.core.memory_ops import (
    MemoryService,
    MutationConflictError,
    validate_idempotency_key,
)
from mem0_sidecar.core.scope import validate_scope_id
from mem0_sidecar.http_adapter.dependencies import get_mem0_client, get_session
from mem0_sidecar.http_adapter.project_scope import (
    ensure_project,
    normalized_payload_for_project,
    resolve_app_id,
    resolve_project_app_id,
    resolve_project_id,
)
from mem0_sidecar.store.models import Project


class _SingleDecodeMemoryRoute(APIRoute):
    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        raw_path = scope.get("raw_path")
        if not isinstance(raw_path, bytes):
            return Match.NONE, {}

        index = 0
        while index < len(raw_path):
            if raw_path[index] != ord("%"):
                index += 1
                continue
            encoded_octet = raw_path[index + 1 : index + 3]
            if len(encoded_octet) != 2 or any(
                byte not in b"0123456789abcdefABCDEF" for byte in encoded_octet
            ):
                return Match.NONE, {}
            index += 3

        try:
            decoded_path = unquote_to_bytes(raw_path).decode("utf-8", "strict")
        except UnicodeDecodeError:
            return Match.NONE, {}
        scope = {**scope, "path": decoded_path}
        return super().matches(scope)


memory_router = APIRouter(route_class=_SingleDecodeMemoryRoute)
SessionDependency = Annotated[Session, Depends(get_session)]
Mem0Dependency = Annotated[Any, Depends(get_mem0_client)]


def _resolve_project_wide(
    request: Request,
    payload: dict[str, Any] | None = None,
) -> bool:
    value: Any = None
    if payload is not None and "project_wide" in payload:
        value = payload["project_wide"]
    elif "project_wide" in request.query_params:
        value = request.query_params["project_wide"]

    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    raise ValueError("project_wide must be a boolean")


def _resolve_memory_app_scope(
    request: Request,
    session: Session,
    *,
    project_id: str,
    payload: dict[str, Any] | None = None,
) -> tuple[str | None, bool]:
    project_wide = _resolve_project_wide(request, payload)
    requested_app_id = resolve_app_id(request, payload)
    if project_wide:
        if requested_app_id is not None:
            raise ValueError("app_id cannot be combined with project_wide")
        if resolve_project_app_id(
            session,
            project_id=project_id,
            request_app_id=None,
        ) is None:
            return None, True
        return None, True
    return (
        resolve_project_app_id(
            session,
            project_id=project_id,
            request_app_id=requested_app_id,
        ),
        False,
    )


def _decode_memory_id(memory_id: str) -> str:
    try:
        decoded = unquote(memory_id, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid memory ID") from exc

    has_traversal_segment = any(
        segment in {".", ".."} for segment in re.split(r"[\\/]", decoded)
    )
    if (
        decoded == "query"
        or has_traversal_segment
        or any(ord(character) < 32 or ord(character) == 127 for character in decoded)
    ):
        raise HTTPException(status_code=400, detail="Invalid memory ID")
    return decoded


@memory_router.post("/v3/memories/add/")
@memory_router.post("/v3/memories/add", include_in_schema=False)
async def add_memory(
    payload: dict[str, Any],
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    try:
        idempotency_key = validate_idempotency_key(
            request.headers.get("Idempotency-Key")
        )
        project_id = validate_scope_id(
            resolve_project_id(request, payload),
            field_name="project_id",
        )
        request_app_id = validate_scope_id(
            resolve_app_id(request, payload),
            field_name="app_id",
            required=False,
        )
        for field_name in ("user_id", "agent_id", "run_id"):
            validate_scope_id(
                payload.get(field_name),
                field_name=field_name,
                required=False,
            )
        ensure_project(
            session,
            request.app.state.settings,
            project_id,
            default_app_id=request_app_id,
        )
        session.commit()
        service = MemoryService(session=session, mem0=mem0)
        result = await service.add_memory(
            project_id=project_id,
            payload=normalized_payload_for_project(request, payload),
            idempotency_key=idempotency_key,
        )
        return result
    except MutationConflictError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
    try:
        project_id = resolve_project_id(request, payload)
        app_id, project_wide = _resolve_memory_app_scope(
            request,
            session,
            project_id=project_id,
            payload=payload,
        )
        if app_id is None and not project_wide:
            raise HTTPException(status_code=404, detail="Project not found")
        if project_wide and session.get(Project, project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        # Scope resolution performs a read and therefore autobegins a transaction.
        # End that request-owned read transaction before the traced operation takes
        # exclusive ownership of the session transaction lifecycle.
        session.rollback()
        service = MemoryService(session=session, mem0=mem0)
        query_payload = dict(payload)
        query_payload.pop("project_wide", None)
        query = parse_explorer_query(
            query_payload,
            allowed_fields=MEMORY_FILTER_FIELDS,
        )
        result = await service.query_memories(
            project_id=project_id,
            app_id=app_id,
            project_wide=project_wide,
            query=query,
        )
        session.commit()
    except HTTPException:
        session.rollback()
        raise
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
    memory_id = _decode_memory_id(memory_id)
    project_id = resolve_project_id(request)
    try:
        request_app_id, project_wide = _resolve_memory_app_scope(
            request,
            session,
            project_id=project_id,
        )
        if request_app_id is None and not project_wide:
            raise HTTPException(status_code=404, detail="Memory not found")
        session.rollback()
        service = MemoryService(session=session, mem0=mem0)
        return await service.get_memory(
            project_id=project_id,
            memory_id=memory_id,
            request_app_id=request_app_id,
            project_wide=project_wide,
        )
    except HTTPException:
        session.rollback()
        raise
    except KeyError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="Memory not found") from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@memory_router.patch("/v1/memories/{memory_id}/")
@memory_router.patch("/v1/memories/{memory_id}", include_in_schema=False)
async def update_memory(
    memory_id: str,
    payload: dict[str, Any],
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    memory_id = _decode_memory_id(memory_id)
    project_id = resolve_project_id(request, payload)
    try:
        request_app_id, project_wide = _resolve_memory_app_scope(
            request,
            session,
            project_id=project_id,
            payload=payload,
        )
        if request_app_id is None and not project_wide:
            raise HTTPException(status_code=404, detail="Memory not found")
        patch = normalized_payload_for_project(request, payload)
        patch.pop("app_id", None)
        patch.pop("project_wide", None)
        service = MemoryService(session=session, mem0=mem0)
        result = await service.update_memory(
            project_id=project_id,
            memory_id=memory_id,
            request_app_id=request_app_id,
            project_wide=project_wide,
            payload=patch,
        )
        return result
    except HTTPException:
        session.rollback()
        raise
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
    memory_id = _decode_memory_id(memory_id)
    project_id = resolve_project_id(request)
    try:
        request_app_id, project_wide = _resolve_memory_app_scope(
            request,
            session,
            project_id=project_id,
        )
        if request_app_id is None and not project_wide:
            raise HTTPException(status_code=404, detail="Memory not found")
        session.rollback()
        service = MemoryService(session=session, mem0=mem0)
        return await service.get_memory_history(
            project_id=project_id,
            memory_id=memory_id,
            request_app_id=request_app_id,
            project_wide=project_wide,
        )
    except HTTPException:
        session.rollback()
        raise
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
    try:
        project_id = validate_scope_id(
            resolve_project_id(request, payload),
            field_name="project_id",
        )
        validated_path_project_id = validate_scope_id(
            path_project_id,
            field_name="project_id",
        )
        if project_id != validated_path_project_id:
            raise HTTPException(status_code=403, detail="Project scope mismatch")

        requested_app_id = validate_scope_id(
            resolve_app_id(request, payload),
            field_name="app_id",
            required=False,
        )
        app_id = resolve_project_app_id(
            session,
            project_id=project_id,
            request_app_id=requested_app_id,
        )
        if app_id is None:
            raise HTTPException(status_code=404, detail="Project not found")
        app_id = validate_scope_id(app_id, field_name="app_id")

        adopt_unscoped = payload.get("adopt_unscoped", False)
        if not isinstance(adopt_unscoped, bool):
            raise ValueError("adopt_unscoped must be a boolean")
        result = await MemoryService(session=session, mem0=mem0).reconcile_memories(
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
    except HTTPException:
        session.rollback()
        raise
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
    memory_id = _decode_memory_id(memory_id)
    project_id = resolve_project_id(request)
    try:
        request_app_id, project_wide = _resolve_memory_app_scope(
            request,
            session,
            project_id=project_id,
        )
        if request_app_id is None and not project_wide:
            raise HTTPException(status_code=404, detail="Memory not found")
        service = MemoryService(session=session, mem0=mem0)
        result = await service.delete_memory(
            project_id=project_id,
            memory_id=memory_id,
            request_app_id=request_app_id,
            project_wide=project_wide,
        )
        return result
    except HTTPException:
        session.rollback()
        raise
    except KeyError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="Memory not found") from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        session.rollback()
        raise
