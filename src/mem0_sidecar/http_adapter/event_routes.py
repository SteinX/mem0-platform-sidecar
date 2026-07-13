from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from mem0_sidecar.core.events import EventService, event_to_trace_dict
from mem0_sidecar.core.explorer_filters import parse_explorer_query
from mem0_sidecar.core.scope import validate_scope_id
from mem0_sidecar.http_adapter.dependencies import get_session
from mem0_sidecar.http_adapter.project_scope import (
    resolve_project_app_id,
    resolve_project_id,
)
from mem0_sidecar.store.models import EventStatus
from mem0_sidecar.store.repositories import (
    EVENT_SCAN_LIMIT,
    EventQuery,
    EventRepository,
)

event_router = APIRouter()
SessionDependency = Annotated[Session, Depends(get_session)]
_DISPLAY_OPERATIONS = {
    "ADD": "memory.add",
    "SEARCH": "memory.search",
    "GET_ALL": "memory.list",
}
_ENTITY_FILTER_FIELDS = frozenset({"user_id", "agent_id", "app_id", "run_id"})
_QUERY_KEYS = frozenset(
    {
        "project_id",
        "app_id",
        "operation",
        "statuses",
        "has_results",
        "date_range",
        "entity_filters",
        "page",
        "page_size",
    }
)
_DATE_RANGE_KEYS = frozenset({"from", "to"})


def _explicit_scope_value(
    request: Request,
    payload: dict[str, Any] | None,
    field_name: str,
) -> str | None:
    if payload is not None and field_name in payload:
        return validate_scope_id(payload[field_name], field_name=field_name)
    if field_name in request.query_params:
        return validate_scope_id(
            request.query_params.get(field_name),
            field_name=field_name,
        )
    return None


def _resolve_event_scope(
    request: Request,
    session: Session,
    payload: dict[str, Any] | None = None,
) -> tuple[str, str]:
    explicit_project_id = _explicit_scope_value(
        request,
        payload,
        "project_id",
    )
    project_id = explicit_project_id or validate_scope_id(
        request.app.state.settings.default_project_id,
        field_name="project_id",
    )
    requested_app_id = _explicit_scope_value(request, payload, "app_id")
    app_id = resolve_project_app_id(
        session,
        project_id=project_id,
        request_app_id=requested_app_id,
    )
    if app_id is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project_id, validate_scope_id(app_id, field_name="app_id")


def _parse_event_query(payload: dict[str, Any]) -> EventQuery:
    unknown_keys = set(payload) - _QUERY_KEYS
    if unknown_keys:
        names = ", ".join(sorted(str(key) for key in unknown_keys))
        raise ValueError(f"unknown query fields: {names}")

    raw_date_range = payload.get("date_range")
    if isinstance(raw_date_range, dict):
        unknown_date_keys = set(raw_date_range) - _DATE_RANGE_KEYS
        if unknown_date_keys:
            names = ", ".join(sorted(str(key) for key in unknown_date_keys))
            raise ValueError(f"unknown date_range fields: {names}")

    raw_operation = payload.get("operation")
    if raw_operation is None:
        operation = None
    elif type(raw_operation) is str and raw_operation in _DISPLAY_OPERATIONS:
        operation = _DISPLAY_OPERATIONS[raw_operation]
    else:
        raise ValueError("operation must be one of ADD, GET_ALL, SEARCH")

    raw_statuses = payload.get("statuses", [])
    if not isinstance(raw_statuses, list):
        raise ValueError("statuses must be a list")
    if len(raw_statuses) > len(EventStatus):
        raise ValueError(f"statuses must contain at most {len(EventStatus)} items")
    statuses: list[EventStatus] = []
    seen_statuses: set[EventStatus] = set()
    for index, raw_status in enumerate(raw_statuses):
        if type(raw_status) is not str:
            raise ValueError(f"statuses[{index}] is invalid")
        try:
            status = EventStatus(raw_status)
        except ValueError as exc:
            raise ValueError(f"statuses[{index}] is invalid") from exc
        if status in seen_statuses:
            raise ValueError(f"statuses[{index}] is duplicated")
        seen_statuses.add(status)
        statuses.append(status)

    has_results = payload.get("has_results")
    if has_results is not None and type(has_results) is not bool:
        raise ValueError("has_results must be a boolean")

    raw_entity_filters = payload.get("entity_filters", {})
    if not isinstance(raw_entity_filters, dict):
        raise ValueError("entity_filters must be an object")
    entity_filters: dict[str, str] = {}
    for field_name, value in raw_entity_filters.items():
        if field_name not in _ENTITY_FILTER_FIELDS:
            raise ValueError(f"entity_filters.{field_name} is not allowed")
        entity_filters[field_name] = validate_scope_id(
            value,
            field_name=field_name,
        )

    shared_query = parse_explorer_query(
        {
            "date_range": payload.get("date_range"),
            "page": payload.get("page", 1),
            "page_size": payload.get("page_size", 50),
        },
        allowed_fields=set(),
    )
    if (shared_query.page - 1) * shared_query.page_size >= EVENT_SCAN_LIMIT:
        raise ValueError("page exceeds 5000-record event scan horizon")
    return EventQuery(
        operation=operation,
        statuses=tuple(statuses),
        has_results=has_results,
        from_at=shared_query.date_range.from_at,
        to_at=shared_query.date_range.to_at,
        entity_filters=entity_filters,
        page=shared_query.page,
        page_size=shared_query.page_size,
    )


@event_router.post("/v1/events/query")
def query_events(
    payload: dict[str, Any],
    request: Request,
    session: SessionDependency,
) -> dict[str, Any]:
    try:
        query = _parse_event_query(payload)
        project_id, app_id = _resolve_event_scope(request, session, payload)
        page = EventRepository(session).query_project_events(
            project_id,
            app_id,
            query,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "results": [event_to_trace_dict(event) for event in page.items],
        "total": page.total,
        "page": query.page,
        "page_size": query.page_size,
        "has_more": query.page * query.page_size < page.total,
        "timeline": page.buckets,
    }


@event_router.get("/v1/events")
@event_router.get("/v1/events/", include_in_schema=False)
def list_events(
    request: Request,
    session: SessionDependency,
) -> dict[str, Any]:
    project_id = resolve_project_id(request)
    service = EventService(EventRepository(session))
    try:
        return {"results": service.list_project_events(project_id)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@event_router.get("/v1/event/{event_id}")
@event_router.get("/v1/event/{event_id}/", include_in_schema=False)
def get_event(
    event_id: str,
    request: Request,
    session: SessionDependency,
) -> dict[str, Any]:
    service = EventService(EventRepository(session))
    try:
        project_id, app_id = _resolve_event_scope(request, session)
        return service.get_project_event(project_id, app_id, event_id)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="Event not found") from exc
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Event not found") from exc
