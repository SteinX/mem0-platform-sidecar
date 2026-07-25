from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.core.scope import validate_scope_id
from mem0_sidecar.store.models import Project
from mem0_sidecar.store.repositories import (
    MemoryIndexRepository,
    ProjectRepository,
)

_SIDECAR_PROJECT_ID_METADATA_KEY = "_mem0_sidecar_project_id"
_SIDECAR_APP_ID_METADATA_KEY = "_mem0_sidecar_app_id"


@dataclass(frozen=True)
class CompatibleScope:
    project_id: str
    app_id: str
    app_is_explicit: bool


def _non_empty_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def enforce_compatible_scope_boundary(
    request: Request,
    payload: dict[str, Any] | None = None,
) -> None:
    """Prevent ordinary Core principals from selecting sidecar tenant scope."""

    principal = request.state.client_principal
    if principal.role in {"admin", "system"}:
        return

    body = payload or {}
    filters = body.get("filters")
    if not isinstance(filters, dict):
        filters = {}
    metadata = body.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    selectors = (
        request.headers.get("X-Mem0-Project-ID"),
        request.headers.get("X-Mem0-App-ID"),
        body.get("project_id"),
        body.get("app_id"),
        request.query_params.get("project_id"),
        request.query_params.get("app_id"),
        filters.get("project_id"),
        filters.get("app_id"),
        metadata.get(_SIDECAR_PROJECT_ID_METADATA_KEY),
        metadata.get(_SIDECAR_APP_ID_METADATA_KEY),
    )
    if any(_non_empty_string(selector) is not None for selector in selectors):
        raise HTTPException(
            status_code=403,
            detail="Project and app scope overrides require an admin principal",
        )


def resolve_compatible_scope(
    request: Request,
    session: Session,
    payload: dict[str, Any] | None = None,
) -> CompatibleScope:
    """Resolve transparent-ingress scope without client-side sidecar knowledge."""

    body = payload or {}
    filters = body.get("filters")
    if not isinstance(filters, dict):
        filters = {}
    metadata = body.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    project_candidates = (
        _non_empty_string(request.headers.get("X-Mem0-Project-ID")),
        _non_empty_string(body.get("project_id")),
        _non_empty_string(request.query_params.get("project_id")),
        _non_empty_string(filters.get("project_id")),
        _non_empty_string(metadata.get(_SIDECAR_PROJECT_ID_METADATA_KEY)),
        request.app.state.settings.default_project_id,
    )
    project_id = next(
        candidate for candidate in project_candidates if candidate is not None
    )
    project_id = validate_scope_id(project_id, field_name="project_id")

    project = session.get(Project, project_id)
    default_app_id = (
        project.default_app_id
        if project is not None and project.default_app_id
        else project_id
    )
    app_candidates = (
        _non_empty_string(request.headers.get("X-Mem0-App-ID")),
        _non_empty_string(body.get("app_id")),
        _non_empty_string(request.query_params.get("app_id")),
        _non_empty_string(filters.get("app_id")),
        _non_empty_string(metadata.get(_SIDECAR_APP_ID_METADATA_KEY)),
    )
    explicit_app_id = next(
        (candidate for candidate in app_candidates if candidate is not None),
        None,
    )
    app_id = explicit_app_id or default_app_id
    app_id = validate_scope_id(app_id, field_name="app_id")
    return CompatibleScope(
        project_id=project_id,
        app_id=app_id,
        app_is_explicit=explicit_app_id is not None,
    )


def resolve_compatible_memory_scope(
    request: Request,
    session: Session,
    *,
    memory_id: str,
    payload: dict[str, Any] | None = None,
) -> CompatibleScope:
    scope = resolve_compatible_scope(request, session, payload)
    if scope.app_is_explicit:
        return scope
    principal = request.state.client_principal
    if principal.role not in {"admin", "system"}:
        return scope
    memory = MemoryIndexRepository(session).get_memory(
        project_id=scope.project_id,
        mem0_memory_id=memory_id,
    )
    if memory is None or not memory.app_id:
        return scope
    return CompatibleScope(
        project_id=scope.project_id,
        app_id=validate_scope_id(memory.app_id, field_name="app_id"),
        app_is_explicit=False,
    )


def resolve_app_id(
    request: Request,
    payload: dict[str, Any] | None = None,
) -> str | None:
    if payload:
        app_id = payload.get("app_id")
        if isinstance(app_id, str) and app_id:
            return app_id

    query_app_id = request.query_params.get("app_id")
    if query_app_id:
        return query_app_id

    return None


def resolve_project_id(request: Request, payload: dict[str, Any] | None = None) -> str:
    if payload:
        project_id = payload.get("project_id")
        if isinstance(project_id, str) and project_id:
            return project_id

    query_project_id = request.query_params.get("project_id")
    if query_project_id:
        return query_project_id

    if payload:
        app_id = payload.get("app_id")
        if isinstance(app_id, str) and app_id:
            return app_id

    query_app_id = request.query_params.get("app_id")
    if query_app_id:
        return query_app_id

    return request.app.state.settings.default_project_id


def resolve_project_app_id(
    session: Session,
    *,
    project_id: str,
    request_app_id: str | None,
) -> str | None:
    project = session.get(Project, project_id)
    if project is None:
        return None
    if request_app_id:
        return request_app_id
    return project.default_app_id


def normalized_payload_for_project(
    request: Request, payload: dict[str, Any]
) -> dict[str, Any]:
    normalized_payload = dict(payload)
    normalized_payload.pop("project_id", None)
    query_app_id = resolve_app_id(request)
    if query_app_id and "app_id" not in normalized_payload:
        normalized_payload["app_id"] = query_app_id
    return normalized_payload


def ensure_project(
    session: Session,
    settings: SidecarSettings,
    project_id: str,
    default_app_id: str | None = None,
) -> None:
    validated_project_id = validate_scope_id(project_id, field_name="project_id")
    validated_app_id = validate_scope_id(
        default_app_id,
        field_name="app_id",
        required=False,
    )
    ProjectRepository(session).upsert_default_project(
        project_id=validated_project_id,
        name=validated_project_id,
        mem0_base_url=settings.mem0_base_url,
        default_app_id=validated_app_id,
    )
