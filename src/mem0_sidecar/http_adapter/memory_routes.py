import re
from typing import Annotated, Any, Literal
from urllib.parse import unquote, unquote_to_bytes

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session
from starlette.routing import Match
from starlette.types import Scope

from mem0_sidecar.core.explorer_filters import (
    MEMORY_FILTER_FIELDS,
    parse_explorer_query,
)
from mem0_sidecar.core.memory_ops import (
    MemoryProjectionConflictError,
    MemoryService,
    MutationConflictError,
    validate_idempotency_key,
)
from mem0_sidecar.core.memory_scan import MemoryScanConflictError, MemoryScanService
from mem0_sidecar.core.memory_scan_types import (
    JsonObject,
    MemoryScanFilters,
    MemoryScanRequest,
    MemoryScanValidationError,
)
from mem0_sidecar.core.scope import validate_scope_id
from mem0_sidecar.http_adapter.dependencies import (
    get_mem0_client,
    get_session,
    require_client_principal,
)
from mem0_sidecar.http_adapter.project_scope import (
    enforce_compatible_scope_boundary,
    ensure_project,
    normalized_payload_for_project,
    resolve_app_id,
    resolve_project_app_id,
    resolve_project_id,
)
from mem0_sidecar.request_attribution import RequestAttribution
from mem0_sidecar.store.models import Project
from mem0_sidecar.store.repositories import EventRepository


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


memory_router = APIRouter(
    route_class=_SingleDecodeMemoryRoute,
    dependencies=[Depends(require_client_principal)],
)
SessionDependency = Annotated[Session, Depends(get_session)]
Mem0Dependency = Annotated[Any, Depends(get_mem0_client)]


class MemoryScanFiltersPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: str | None = None
    agent_id: str | None = None
    run_id: str | None = None
    type: str | None = None


class MemoryScanPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str | None = None
    app_id: str | None = None
    project_wide: bool = False
    filters: MemoryScanFiltersPayload = Field(default_factory=MemoryScanFiltersPayload)
    mode: Literal["page", "count"]
    page_size: int | None = Field(default=None, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)
    include_expired: bool = False

    @model_validator(mode="after")
    def validate_scan_shape(self) -> "MemoryScanPayload":
        if self.project_wide and self.app_id is not None:
            raise ValueError("app_id cannot be combined with project_wide")
        if self.mode == "count" and self.model_fields_set.intersection(
            {"page_size", "cursor"}
        ):
            raise ValueError("count mode does not accept page_size or cursor")
        return self


class MemoryScanResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    protocol: Literal["cursor-v1"]
    results: list[JsonObject]
    total: int
    count_basis: Literal["sidecar_projection"]
    next_cursor: str | None
    has_more: bool
    stale_skipped: int


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


def _enforce_platform_scope_boundary(
    request: Request,
    payload: dict[str, Any] | None = None,
) -> None:
    enforce_compatible_scope_boundary(request, payload)
    principal = request.state.client_principal
    if principal.role in {"admin", "system"}:
        return
    try:
        project_wide = _resolve_project_wide(request, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if project_wide:
        raise HTTPException(
            status_code=403,
            detail="Project-wide memory access requires an admin principal",
        )


def _normalized_platform_payload(
    request: Request,
    session: Session,
    *,
    project_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    normalized = normalized_payload_for_project(request, payload)
    app_id = resolve_project_app_id(
        session,
        project_id=project_id,
        request_app_id=resolve_app_id(request, payload),
    )
    if app_id is not None:
        normalized["app_id"] = validate_scope_id(app_id, field_name="app_id")
    return normalized


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
        _enforce_platform_scope_boundary(request, payload)
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
        service_payload = _normalized_platform_payload(
            request,
            session,
            project_id=project_id,
            payload=payload,
        )
        session.commit()
        service = MemoryService(session=session, mem0=mem0)
        result = await service.add_memory(
            project_id=project_id,
            payload=service_payload,
            idempotency_key=idempotency_key,
        )
        event = EventRepository(session).get(result["event"]["id"])
        result["event"]["channel"] = RequestAttribution.from_stored(
            transport=event.request_transport,
            credential_kind=event.credential_kind,
            credential_id=event.credential_id,
            credential_label=event.credential_label,
            credential_prefix=event.credential_prefix,
        ).to_channel_dict()
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
    _enforce_platform_scope_boundary(request, payload)
    project_id = resolve_project_id(request, payload)
    service_payload = _normalized_platform_payload(
        request,
        session,
        project_id=project_id,
        payload=payload,
    )
    session.rollback()
    service = MemoryService(session=session, mem0=mem0)
    return await service.search_memories(
        project_id=project_id,
        payload=service_payload,
    )


@memory_router.post("/v1/memories/query")
async def query_memories(
    payload: dict[str, Any],
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    try:
        _enforce_platform_scope_boundary(request, payload)
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
        try:
            result = await service.query_memories(
                project_id=project_id,
                app_id=app_id,
                project_wide=project_wide,
                query=query,
            )
        except MemoryProjectionConflictError:
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


@memory_router.post("/v1/memories/scan", response_model=MemoryScanResponse)
async def scan_memories(
    payload: MemoryScanPayload,
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> MemoryScanResponse:
    raw_payload = payload.model_dump(exclude_unset=True)
    try:
        _enforce_platform_scope_boundary(request, raw_payload)
        if request.state.client_principal.role in {"admin", "system"} and (
            payload.project_id is None
            or payload.project_wide == (payload.app_id is not None)
        ):
            raise ValueError("exactly one of app_id or project_wide=true is required")
        for field_name, value in (
            ("project_id", payload.project_id),
            ("app_id", payload.app_id),
        ):
            if value is not None:
                validate_scope_id(value, field_name=field_name)
        project_id = validate_scope_id(
            resolve_project_id(request, raw_payload), field_name="project_id"
        )
        for field_name, value in (
            ("user_id", payload.filters.user_id),
            ("agent_id", payload.filters.agent_id),
            ("run_id", payload.filters.run_id),
        ):
            validate_scope_id(value, field_name=field_name, required=False)
        app_id, project_wide = _resolve_memory_app_scope(
            request,
            session,
            project_id=project_id,
            payload=raw_payload,
        )
        if app_id is None and not project_wide:
            raise HTTPException(status_code=404, detail="Project not found")
        if project_wide and session.get(Project, project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        session.rollback()
        result = await MemoryScanService(
            session=session,
            mem0=mem0,
            cursor_secret=request.app.state.memory_cursor_secret,
        ).scan(
            MemoryScanRequest(
                project_id=project_id,
                app_id=app_id,
                project_wide=project_wide,
                filters=MemoryScanFilters(
                    user_id=payload.filters.user_id,
                    agent_id=payload.filters.agent_id,
                    run_id=payload.filters.run_id,
                    type=payload.filters.type,
                ),
                mode=payload.mode,
                page_size=payload.page_size,
                cursor=payload.cursor,
                include_expired=payload.include_expired,
            )
        )
        session.commit()
    except HTTPException:
        session.rollback()
        raise
    except MemoryScanConflictError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (MemoryScanValidationError, ValueError) as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        session.rollback()
        raise

    return MemoryScanResponse(
        protocol=result.protocol,
        results=list(result.results),
        total=result.total,
        count_basis=result.count_basis,
        next_cursor=result.next_cursor,
        has_more=result.has_more,
        stale_skipped=result.stale_skipped,
    )


@memory_router.get("/v1/memories/{memory_id}/")
@memory_router.get("/v1/memories/{memory_id}", include_in_schema=False)
async def get_memory(
    memory_id: str,
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    memory_id = _decode_memory_id(memory_id)
    _enforce_platform_scope_boundary(request)
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
    try:
        _enforce_platform_scope_boundary(request, payload)
        project_id = resolve_project_id(request, payload)
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
    _enforce_platform_scope_boundary(request)
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
        principal = request.state.client_principal
        if principal.role not in {"admin", "system"}:
            raise HTTPException(
                status_code=403,
                detail="Memory reconciliation requires an admin principal",
            )
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
    _enforce_platform_scope_boundary(request)
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
