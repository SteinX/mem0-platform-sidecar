from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from mem0_sidecar.core.events import EventService
from mem0_sidecar.http_adapter.dependencies import get_session
from mem0_sidecar.http_adapter.project_scope import resolve_project_id
from mem0_sidecar.store.repositories import EventRepository

event_router = APIRouter()
SessionDependency = Annotated[Session, Depends(get_session)]


@event_router.get("/v1/events")
@event_router.get("/v1/events/", include_in_schema=False)
def list_events(
    request: Request,
    session: SessionDependency,
) -> dict[str, Any]:
    project_id = resolve_project_id(request)
    service = EventService(EventRepository(session))
    return {"results": service.list_project_events(project_id)}


@event_router.get("/v1/event/{event_id}")
@event_router.get("/v1/event/{event_id}/", include_in_schema=False)
def get_event(
    event_id: str,
    request: Request,
    session: SessionDependency,
) -> dict[str, Any]:
    project_id = resolve_project_id(request)
    service = EventService(EventRepository(session))
    try:
        return service.get_project_event(project_id, event_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Event not found") from exc
