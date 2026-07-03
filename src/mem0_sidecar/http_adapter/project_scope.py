from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.store.repositories import ProjectRepository


def resolve_app_id(request: Request, payload: dict[str, Any] | None = None) -> str | None:
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
    ProjectRepository(session).upsert_default_project(
        project_id=project_id,
        name=project_id,
        mem0_base_url=settings.mem0_base_url,
        default_app_id=default_app_id,
    )
