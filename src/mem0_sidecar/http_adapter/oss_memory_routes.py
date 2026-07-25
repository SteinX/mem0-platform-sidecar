import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from mem0_sidecar.core.memory_ops import (
    MemoryService,
    MutationConflictError,
    validate_idempotency_key,
)
from mem0_sidecar.core.scope import validate_scope_id
from mem0_sidecar.http_adapter.dependencies import (
    get_mem0_client,
    get_session,
    require_client_principal,
)
from mem0_sidecar.http_adapter.memory_routes import (
    _decode_memory_id,
    _SingleDecodeMemoryRoute,
)
from mem0_sidecar.http_adapter.oss_response_adapter import (
    as_oss_add_response,
    as_oss_mutation_response,
    as_oss_search_response,
)
from mem0_sidecar.http_adapter.project_scope import (
    enforce_compatible_scope_boundary,
    ensure_project,
    resolve_compatible_memory_scope,
    resolve_compatible_scope,
)
from mem0_sidecar.mem0_client.client import Mem0UpstreamError
from mem0_sidecar.observability import get_request_id
from mem0_sidecar.store.repositories import EventRepository, MemoryIndexRepository

oss_memory_router = APIRouter(
    route_class=_SingleDecodeMemoryRoute,
    dependencies=[Depends(require_client_principal)],
)
SessionDependency = Annotated[Session, Depends(get_session)]
Mem0Dependency = Annotated[Any, Depends(get_mem0_client)]


class _CompatibleBadRequest(ValueError):
    pass


def _raise_compatible_upstream_error(exc: Mem0UpstreamError) -> None:
    if exc.status_code not in {400, 404, 422}:
        raise exc
    detail: Any = "Upstream request rejected"
    if exc.response_text:
        try:
            document = json.loads(exc.response_text)
        except (TypeError, ValueError):
            document = None
        if isinstance(document, dict) and isinstance(
            document.get("detail"),
            (str, list, dict),
        ):
            detail = document["detail"]
    raise HTTPException(status_code=exc.status_code, detail=detail) from exc


def _trace_missing_compatible_memory(
    session: Session,
    *,
    project_id: str,
    app_id: str,
    memory_id: str,
    operation: str,
) -> None:
    memory = MemoryIndexRepository(session).get_memory(
        project_id=project_id,
        mem0_memory_id=memory_id,
        app_id=app_id,
    )
    if memory is not None:
        return
    event_repo = EventRepository(session)
    event = event_repo.create_event(
        project_id=project_id,
        app_id=app_id,
        operation=operation,
        request={"app_id": app_id, "memory_id": memory_id},
        subject_type="memory",
        subject_id=memory_id,
        correlation_id=get_request_id(),
    )
    event_repo.mark_failed(event.id, error={"message": "Memory not found"})
    session.commit()
    raise HTTPException(status_code=404, detail="Memory not found")


def _validate_add_payload(payload: dict[str, Any]) -> None:
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list")
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError(f"messages[{index}] must be an object")
        for field_name in ("role", "content"):
            if not isinstance(message.get(field_name), str):
                raise ValueError(
                    f"messages[{index}].{field_name} must be a string"
                )
    entity_ids = [
        validate_scope_id(
            payload.get(field_name),
            field_name=field_name,
            required=False,
        )
        for field_name in ("user_id", "agent_id", "run_id")
    ]
    if not any(entity_ids):
        raise _CompatibleBadRequest(
            "At least one of user_id, agent_id, or run_id is required"
        )


def _search_service_payload(
    payload: dict[str, Any],
    *,
    app_id: str,
) -> dict[str, Any]:
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise _CompatibleBadRequest("query must be a non-empty string")
    filters = payload.get("filters", {})
    if filters is None:
        filters = {}
    if not isinstance(filters, dict):
        raise ValueError("filters must be an object")

    normalized = dict(payload)
    normalized.pop("project_id", None)
    normalized["app_id"] = app_id
    normalized_filters = dict(filters)
    normalized_filters.pop("project_id", None)
    normalized_filters.pop("app_id", None)
    normalized["filters"] = normalized_filters
    for field_name in ("user_id", "agent_id", "run_id"):
        candidate = normalized.get(field_name, normalized_filters.get(field_name))
        validated = validate_scope_id(
            candidate,
            field_name=field_name,
            required=False,
        )
        if validated is None:
            normalized.pop(field_name, None)
        else:
            normalized[field_name] = validated
    return normalized


def _list_service_payload(
    request: Request,
    *,
    app_id: str,
) -> dict[str, Any]:
    normalized: dict[str, Any] = {"app_id": app_id}
    for field_name in ("user_id", "agent_id", "run_id"):
        value = validate_scope_id(
            request.query_params.get(field_name),
            field_name=field_name,
            required=False,
        )
        if value is not None:
            normalized[field_name] = value

    raw_top_k = request.query_params.get("top_k")
    if raw_top_k is not None:
        try:
            top_k = int(raw_top_k)
        except ValueError as exc:
            raise ValueError("top_k must be an integer") from exc
        if top_k < 0:
            raise ValueError("top_k must be at least 0")
        normalized["top_k"] = top_k

    raw_show_expired = request.query_params.get("show_expired")
    if raw_show_expired is not None:
        lowered = raw_show_expired.lower()
        if lowered not in {"true", "false"}:
            raise ValueError("show_expired must be a boolean")
        normalized["show_expired"] = lowered == "true"
    return normalized


@oss_memory_router.post("/memories")
async def oss_add_memory(
    payload: dict[str, Any],
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    try:
        enforce_compatible_scope_boundary(request, payload)
        _validate_add_payload(payload)
        scope = resolve_compatible_scope(request, session, payload)
        ensure_project(
            session,
            request.app.state.settings,
            scope.project_id,
            default_app_id=scope.app_id,
        )
        session.commit()
        service_payload = dict(payload)
        service_payload.pop("project_id", None)
        service_payload["app_id"] = scope.app_id
        result = await MemoryService(session=session, mem0=mem0).add_memory(
            project_id=scope.project_id,
            payload=service_payload,
            idempotency_key=validate_idempotency_key(
                request.headers.get("Idempotency-Key")
            ),
        )
        return as_oss_add_response(result)
    except MutationConflictError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except _CompatibleBadRequest as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Mem0UpstreamError as exc:
        session.rollback()
        _raise_compatible_upstream_error(exc)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        session.rollback()
        raise


@oss_memory_router.get("/memories")
async def oss_list_memories(
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> Any:
    try:
        enforce_compatible_scope_boundary(request)
        scope = resolve_compatible_scope(request, session)
        service_payload = _list_service_payload(request, app_id=scope.app_id)
        has_entity_scope = any(
            service_payload.get(field_name)
            for field_name in ("user_id", "agent_id", "run_id")
        )
        principal = request.state.client_principal
        if not has_entity_scope and principal.role not in {"admin", "system"}:
            raise HTTPException(
                status_code=403,
                detail="Unscoped memory listing requires an admin principal",
            )
        session.rollback()
        return await MemoryService(session=session, mem0=mem0).list_memories(
            project_id=scope.project_id,
            payload=service_payload,
            preserve_wire_shape=True,
        )
    except HTTPException:
        session.rollback()
        raise
    except Mem0UpstreamError as exc:
        session.rollback()
        _raise_compatible_upstream_error(exc)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        session.rollback()
        raise


@oss_memory_router.delete("/memories")
async def oss_bulk_delete_memories(
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, str]:
    try:
        enforce_compatible_scope_boundary(request)
        principal = request.state.client_principal
        if principal.role not in {"admin", "system"}:
            raise HTTPException(
                status_code=403,
                detail="Bulk memory deletion requires an admin principal",
            )
        scope = resolve_compatible_scope(request, session)
        filters = {
            field_name: request.query_params.get(field_name)
            for field_name in ("user_id", "agent_id", "run_id")
            if request.query_params.get(field_name) is not None
        }
        session.rollback()
        return await MemoryService(
            session=session,
            mem0=mem0,
        ).bulk_delete_memories(
            project_id=scope.project_id,
            app_id=scope.app_id,
            filters=filters,
        )
    except HTTPException:
        session.rollback()
        raise
    except MutationConflictError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Mem0UpstreamError as exc:
        session.rollback()
        _raise_compatible_upstream_error(exc)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        session.rollback()
        raise


@oss_memory_router.post("/search")
async def oss_search_memories(
    payload: dict[str, Any],
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> Any:
    try:
        enforce_compatible_scope_boundary(request, payload)
        scope = resolve_compatible_scope(request, session, payload)
        service_payload = _search_service_payload(payload, app_id=scope.app_id)
        session.rollback()
        result = await MemoryService(session=session, mem0=mem0).search_memories(
            project_id=scope.project_id,
            payload=service_payload,
            preserve_wire_shape=True,
        )
        return as_oss_search_response(result)
    except _CompatibleBadRequest as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Mem0UpstreamError as exc:
        session.rollback()
        _raise_compatible_upstream_error(exc)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        session.rollback()
        raise


@oss_memory_router.get("/memories/{memory_id}")
async def oss_get_memory(
    memory_id: str,
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    memory_id = _decode_memory_id(memory_id)
    try:
        enforce_compatible_scope_boundary(request)
        scope = resolve_compatible_memory_scope(
            request,
            session,
            memory_id=memory_id,
        )
        _trace_missing_compatible_memory(
            session,
            project_id=scope.project_id,
            app_id=scope.app_id,
            memory_id=memory_id,
            operation="memory.get",
        )
        session.rollback()
        return await MemoryService(session=session, mem0=mem0).get_memory(
            project_id=scope.project_id,
            memory_id=memory_id,
            request_app_id=scope.app_id,
            trace_event=True,
        )
    except KeyError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="Memory not found") from exc
    except Mem0UpstreamError as exc:
        session.rollback()
        _raise_compatible_upstream_error(exc)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        session.rollback()
        raise


@oss_memory_router.get("/memories/{memory_id}/history")
async def oss_get_memory_history(
    memory_id: str,
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> Any:
    memory_id = _decode_memory_id(memory_id)
    try:
        enforce_compatible_scope_boundary(request)
        scope = resolve_compatible_memory_scope(
            request,
            session,
            memory_id=memory_id,
        )
        _trace_missing_compatible_memory(
            session,
            project_id=scope.project_id,
            app_id=scope.app_id,
            memory_id=memory_id,
            operation="memory.history",
        )
        session.rollback()
        return await MemoryService(
            session=session,
            mem0=mem0,
        ).get_memory_history(
            project_id=scope.project_id,
            memory_id=memory_id,
            request_app_id=scope.app_id,
            trace_event=True,
            preserve_wire_shape=True,
        )
    except KeyError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="Memory not found") from exc
    except Mem0UpstreamError as exc:
        session.rollback()
        _raise_compatible_upstream_error(exc)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        session.rollback()
        raise


@oss_memory_router.put("/memories/{memory_id}")
async def oss_update_memory(
    memory_id: str,
    payload: dict[str, Any],
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    memory_id = _decode_memory_id(memory_id)
    try:
        enforce_compatible_scope_boundary(request, payload)
        scope = resolve_compatible_memory_scope(
            request,
            session,
            memory_id=memory_id,
            payload=payload,
        )
        _trace_missing_compatible_memory(
            session,
            project_id=scope.project_id,
            app_id=scope.app_id,
            memory_id=memory_id,
            operation="memory.update",
        )
        patch = dict(payload)
        patch.pop("project_id", None)
        patch.pop("app_id", None)
        session.rollback()
        return await MemoryService(session=session, mem0=mem0).update_memory(
            project_id=scope.project_id,
            memory_id=memory_id,
            request_app_id=scope.app_id,
            payload=patch,
            return_upstream_response=True,
        )
    except MutationConflictError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="Memory not found") from exc
    except Mem0UpstreamError as exc:
        session.rollback()
        _raise_compatible_upstream_error(exc)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        session.rollback()
        raise


@oss_memory_router.delete("/memories/{memory_id}")
async def oss_delete_memory(
    memory_id: str,
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    memory_id = _decode_memory_id(memory_id)
    try:
        enforce_compatible_scope_boundary(request)
        scope = resolve_compatible_memory_scope(
            request,
            session,
            memory_id=memory_id,
        )
        session.rollback()
        result = await MemoryService(session=session, mem0=mem0).delete_memory(
            project_id=scope.project_id,
            memory_id=memory_id,
            request_app_id=scope.app_id,
        )
        return as_oss_mutation_response(result)
    except MutationConflictError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="Memory not found") from exc
    except Mem0UpstreamError as exc:
        session.rollback()
        _raise_compatible_upstream_error(exc)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        session.rollback()
        raise
