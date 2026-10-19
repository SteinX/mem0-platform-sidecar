from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from mem0_sidecar.core.ranking import (
    RankingPolicy,
    RankingPolicyConflict,
    RankingSettings,
    get_ranking_policy,
    set_ranking_policy,
)
from mem0_sidecar.http_adapter.dependencies import get_session
from mem0_sidecar.http_adapter.project_scope import ensure_project

ranking_router = APIRouter()
SessionDependency = Annotated[Session, Depends(get_session)]


class RankingPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_version: int = Field(ge=0, strict=True)
    settings: RankingSettings


@ranking_router.get("/v1/projects/{project_id}/ranking-policy")
def read_policy(
    project_id: str,
    request: Request,
    session: SessionDependency,
) -> RankingPolicy:
    ensure_project(session, request.app.state.settings, project_id)
    return get_ranking_policy(session, project_id)


@ranking_router.put("/v1/projects/{project_id}/ranking-policy")
def update_policy(
    project_id: str,
    payload: RankingPolicyUpdate,
    request: Request,
    session: SessionDependency,
) -> RankingPolicy:
    ensure_project(session, request.app.state.settings, project_id)
    try:
        result = set_ranking_policy(
            session, project_id, payload.expected_version, payload.settings,
        )
    except RankingPolicyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return result
