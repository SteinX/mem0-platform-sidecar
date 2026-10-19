from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from mem0_sidecar.core.extraction_policy import (
    ExtractionPolicyConflict,
    ExtractionPolicyView,
    ExtractionSettings,
    get_extraction_policy,
    set_extraction_policy,
)
from mem0_sidecar.core.scope import validate_scope_id
from mem0_sidecar.http_adapter.dependencies import get_session
from mem0_sidecar.http_adapter.project_scope import ensure_project
from mem0_sidecar.store.repositories import CategoryRepository

extraction_policy_router = APIRouter()
SessionDependency = Annotated[Session, Depends(get_session)]


def _validate_app_id(value: str) -> str:
    validate_scope_id(value, field_name="app_id")
    return value


AppId = Annotated[str, AfterValidator(_validate_app_id)]


class ExtractionPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_version: int = Field(ge=0, strict=True)
    settings: ExtractionSettings


@extraction_policy_router.get("/v1/projects/{project_id}/extraction-policy")
def read_policy(
    project_id: str,
    request: Request,
    session: SessionDependency,
    app_id: Annotated[AppId | None, Query()] = None,
) -> ExtractionPolicyView:
    ensure_project(session, request.app.state.settings, project_id)
    return get_extraction_policy(session, project_id, app_id)


@extraction_policy_router.put("/v1/projects/{project_id}/extraction-policy")
def update_policy(
    project_id: str,
    payload: ExtractionPolicyUpdate,
    request: Request,
    session: SessionDependency,
    app_id: Annotated[AppId | None, Query()] = None,
) -> ExtractionPolicyView:
    ensure_project(session, request.app.state.settings, project_id)
    names = {
        category.name
        for category in CategoryRepository(session).list_project_categories(project_id)
        if category.enabled
    }
    if not payload.settings.temporary_types.keys() <= names:
        raise HTTPException(422, "Temporary types must reference enabled categories")
    try:
        result = set_extraction_policy(
            session,
            project_id=project_id,
            app_id=app_id,
            expected_version=payload.expected_version,
            settings=payload.settings,
        )
    except ExtractionPolicyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return result
