from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from mem0_sidecar.http_adapter.dependencies import get_session
from mem0_sidecar.http_adapter.project_scope import ensure_project
from mem0_sidecar.store.repositories import ServiceCapabilityRepository

capability_router = APIRouter()
SessionDependency = Annotated[Session, Depends(get_session)]


@capability_router.post(
    "/v1/projects/{project_id}/capabilities/bridge-routing/heartbeat"
)
def bridge_routing_heartbeat(
    project_id: str,
    payload: dict[str, Any],
    request: Request,
    session: SessionDependency,
) -> dict[str, object]:
    ensure_project(session, request.app.state.settings, project_id)
    if set(payload) != {
        "instance_id",
        "bridge_version",
        "routes_reads",
        "routes_writes",
    }:
        raise HTTPException(status_code=422, detail="invalid capability heartbeat")
    try:
        result = ServiceCapabilityRepository(session).record_bridge_heartbeat(
            project_id=project_id,
            instance_id=payload["instance_id"],
            bridge_version=payload["bridge_version"],
            routes_reads=payload["routes_reads"],
            routes_writes=payload["routes_writes"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail="invalid capability heartbeat"
        ) from exc
    session.commit()
    return result
