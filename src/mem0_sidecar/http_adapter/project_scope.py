from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.core.scope import validate_scope_id
from mem0_sidecar.store.models import Project
from mem0_sidecar.store.repositories import ProjectRepository


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
