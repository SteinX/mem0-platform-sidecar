from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.store.repositories import ProjectRepository


def resolve_project_id(request: Request, payload: dict[str, Any] | None = None) -> str:
    values: list[str] = []
    if payload:
        for key in ("project_id", "app_id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                values.append(value)

    for key in ("project_id", "app_id"):
        value = request.query_params.get(key)
        if value:
            values.append(value)

    if not values:
        return request.app.state.settings.default_project_id

    unique_values = set(values)
    if len(unique_values) > 1:
        raise HTTPException(
            status_code=400,
            detail="project_id and app_id must match when both are provided",
        )

    return values[0]


def normalized_payload_for_project(
    request: Request, payload: dict[str, Any]
) -> dict[str, Any]:
    project_id = resolve_project_id(request, payload)
    normalized_payload = dict(payload)
    normalized_payload.pop("project_id", None)
    normalized_payload["app_id"] = project_id
    return normalized_payload


def ensure_project(
    session: Session,
    settings: SidecarSettings,
    project_id: str,
) -> None:
    ProjectRepository(session).upsert_default_project(
        project_id=project_id,
        name=project_id,
        mem0_base_url=settings.mem0_base_url,
    )
