import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from mem0_sidecar.http_adapter.dependencies import get_session
from mem0_sidecar.http_adapter.project_scope import ensure_project, resolve_project_id
from mem0_sidecar.store.models import Event

event_router = APIRouter()


def _event_to_dict(event: Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "project_id": event.project_id,
        "operation": event.operation,
        "status": event.status,
        "subject_type": event.subject_type,
        "subject_id": event.subject_id,
        "request": json.loads(event.request_json),
        "response": json.loads(event.response_json),
        "error": json.loads(event.error_json),
    }


@event_router.get("/v1/events")
@event_router.get("/v1/events/", include_in_schema=False)
def list_events(
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    project_id = resolve_project_id(request)
    ensure_project(session, request.app.state.settings, project_id)
    session.commit()
    events = session.scalars(
        select(Event)
        .where(Event.project_id == project_id)
        .order_by(Event.created_at, Event.id)
    ).all()
    return {"results": [_event_to_dict(event) for event in events]}


@event_router.get("/v1/event/{event_id}")
@event_router.get("/v1/event/{event_id}/", include_in_schema=False)
def get_event(
    event_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    project_id = resolve_project_id(request)
    ensure_project(session, request.app.state.settings, project_id)
    session.commit()
    event = session.scalar(
        select(Event).where(Event.project_id == project_id, Event.id == event_id)
    )
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return _event_to_dict(event)
