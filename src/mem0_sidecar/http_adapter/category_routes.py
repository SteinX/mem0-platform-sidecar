from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from mem0_sidecar.core.dashboard_categories import (
    CategoryAdminService,
    CategoryValidationError,
)
from mem0_sidecar.http_adapter.dependencies import get_session
from mem0_sidecar.http_adapter.project_scope import ensure_project
from mem0_sidecar.store.repositories import CategoryRepository

category_router = APIRouter()
SessionDependency = Annotated[Session, Depends(get_session)]
INVALID_CATEGORIES_MESSAGE = "Categories must be a list of category objects"


def _extract_categories(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if "categories" not in payload:
        raise CategoryValidationError(INVALID_CATEGORIES_MESSAGE)
    categories = payload["categories"]
    if not isinstance(categories, list):
        raise CategoryValidationError(INVALID_CATEGORIES_MESSAGE)
    if any(not isinstance(item, dict) for item in categories):
        raise CategoryValidationError(INVALID_CATEGORIES_MESSAGE)
    return categories


@category_router.get("/v1/projects/{project_id}/categories")
def list_project_categories(
    project_id: str,
    session: SessionDependency,
) -> dict[str, Any]:
    service = CategoryAdminService(CategoryRepository(session))
    return service.list_categories(project_id)


@category_router.put("/v1/projects/{project_id}/categories")
def replace_project_categories(
    project_id: str,
    payload: dict[str, Any],
    request: Request,
    session: SessionDependency,
) -> dict[str, Any]:
    ensure_project(session, request.app.state.settings, project_id)
    service = CategoryAdminService(CategoryRepository(session))
    try:
        result = service.replace_categories(
            project_id=project_id,
            items=_extract_categories(payload),
        )
    except CategoryValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return result


@category_router.post("/v1/projects/{project_id}/categories", status_code=201)
def create_project_category(
    project_id: str,
    payload: dict[str, Any],
    request: Request,
    session: SessionDependency,
) -> dict[str, Any]:
    ensure_project(session, request.app.state.settings, project_id)
    service = CategoryAdminService(CategoryRepository(session))
    try:
        result = service.create_category(project_id=project_id, item=payload)
    except CategoryValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return result


@category_router.patch("/v1/projects/{project_id}/categories/{category_id}")
def update_project_category(
    project_id: str,
    category_id: str,
    payload: dict[str, Any],
    request: Request,
    session: SessionDependency,
) -> dict[str, Any]:
    ensure_project(session, request.app.state.settings, project_id)
    service = CategoryAdminService(CategoryRepository(session))
    try:
        result = service.update_category(
            project_id=project_id,
            category_id=category_id,
            item=payload,
        )
    except CategoryValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Category not found") from exc
    session.commit()
    return result


@category_router.delete(
    "/v1/projects/{project_id}/categories/{category_id}", status_code=204
)
def delete_project_category(
    project_id: str,
    category_id: str,
    request: Request,
    session: SessionDependency,
) -> Response:
    ensure_project(session, request.app.state.settings, project_id)
    service = CategoryAdminService(CategoryRepository(session))
    try:
        service.delete_category(project_id=project_id, category_id=category_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Category not found") from exc
    session.commit()
    return Response(status_code=204)
