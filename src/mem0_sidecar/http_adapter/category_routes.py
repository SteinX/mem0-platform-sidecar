from collections.abc import Callable
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mem0_sidecar.core.dashboard_categories import (
    CategoryAdminService,
    CategoryValidationError,
)
from mem0_sidecar.http_adapter.dependencies import get_session
from mem0_sidecar.http_adapter.project_scope import ensure_project
from mem0_sidecar.store.models import Category
from mem0_sidecar.store.repositories import CategoryRepository

category_router = APIRouter()
SessionDependency = Annotated[Session, Depends(get_session)]
INVALID_CATEGORIES_MESSAGE = "Categories must be a list of category objects"
CATEGORY_NAME_UNIQUE_MESSAGE = "Category names must be unique per project"
CATEGORY_NAME_UNIQUE_CONSTRAINT = "uq_categories_project_id_name"
MutationResult = TypeVar("MutationResult")


def _is_category_name_unique_violation(exc: IntegrityError) -> bool:
    original = exc.orig
    diagnostic = getattr(original, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None) or getattr(
        original, "constraint_name", None
    )
    if constraint_name is not None:
        return constraint_name == CATEGORY_NAME_UNIQUE_CONSTRAINT

    return (
        "UNIQUE constraint failed: categories.project_id, categories.name"
        in str(original)
    )


def _commit_category_mutation(
    session: Session,
    mutation: Callable[[], MutationResult],
) -> MutationResult:
    try:
        result = mutation()
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if _is_category_name_unique_violation(exc):
            raise HTTPException(
                status_code=400,
                detail=CATEGORY_NAME_UNIQUE_MESSAGE,
            ) from exc
        raise
    return result


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
        items = _extract_categories(payload)

        def replace() -> dict[str, Any]:
            session.execute(delete(Category).where(Category.project_id == project_id))
            return service.replace_categories(project_id=project_id, items=items)

        return _commit_category_mutation(
            session,
            replace,
        )
    except CategoryValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
        return _commit_category_mutation(
            session,
            lambda: service.create_category(project_id=project_id, item=payload),
        )
    except CategoryValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
        return _commit_category_mutation(
            session,
            lambda: service.update_category(
                project_id=project_id,
                category_id=category_id,
                item=payload,
            ),
        )
    except CategoryValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Category not found") from exc


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
        _commit_category_mutation(
            session,
            lambda: service.delete_category(
                project_id=project_id,
                category_id=category_id,
            ),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Category not found") from exc
    return Response(status_code=204)
