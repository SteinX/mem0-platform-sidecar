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
from mem0_sidecar.request_attribution_codec import (
    parse_credential_kind,
    parse_transport,
)
from mem0_sidecar.store.models import EventStatus, Project
from mem0_sidecar.store.repositories import (
    EVENT_SCAN_LIMIT,
    EventChannelFilter,
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
        "project_wide",
        "operation",
        "statuses",
        "has_results",
        "date_range",
        "entity_filters",
        "channel",
        "page",
        "page_size",
    }
)
_DATE_RANGE_KEYS = frozenset({"from", "to"})
_CHANNEL_KEYS = frozenset({"transport", "credential_kind", "credential_id"})


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
) -> tuple[str, str | None]:
    explicit_project_id = _explicit_scope_value(
        request,
        payload,
        "project_id",
    )
    project_id = explicit_project_id or validate_scope_id(
        request.app.state.settings.default_project_id,
        field_name="project_id",
    )
    if project_id is None:
        raise ValueError("project_id is required")
    requested_app_id = _explicit_scope_value(request, payload, "app_id")
    project_wide = _resolve_project_wide(request, payload)
    if project_wide:
        if requested_app_id is not None:
            raise ValueError("app_id cannot be combined with project_wide")
        if session.get(Project, project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return project_id, None
    app_id = resolve_project_app_id(
        session,
        project_id=project_id,
        request_app_id=requested_app_id,
    )
    if app_id is None:
        raise HTTPException(status_code=404, detail="Project not found")
    validated_app_id = validate_scope_id(app_id, field_name="app_id")
    if validated_app_id is None:
        raise ValueError("app_id is required")
    return project_id, validated_app_id


def _resolve_project_wide(
    request: Request,
    payload: dict[str, Any] | None = None,
) -> bool:
    value: Any = None
    if payload is not None and "project_wide" in payload:
        value = payload["project_wide"]
    elif "project_wide" in request.query_params:
        value = request.query_params["project_wide"]

    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    raise ValueError("project_wide must be a boolean")


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
        entity_id = validate_scope_id(
            value,
            field_name=field_name,
        )
        if entity_id is None:
            raise ValueError(f"entity_filters.{field_name} is required")
        entity_filters[field_name] = entity_id

    raw_channel = payload.get("channel")
    if raw_channel is None:
        channel = None
    elif not isinstance(raw_channel, dict):
        raise ValueError("channel must be an object")
    else:
        unknown_channel_keys = set(raw_channel) - _CHANNEL_KEYS
        if unknown_channel_keys:
            names = ", ".join(sorted(str(key) for key in unknown_channel_keys))
            raise ValueError(f"unknown channel fields: {names}")
        if "transport" not in raw_channel or "credential_kind" not in raw_channel:
            raise ValueError(
                "channel requires transport and credential_kind"
            )
        raw_transport = raw_channel.get("transport")
        if not isinstance(raw_transport, str):
            raise ValueError("channel transport must be a string")
        raw_credential_kind = raw_channel.get("credential_kind")
        if not isinstance(raw_credential_kind, str):
            raise ValueError("channel credential_kind must be a string")
        raw_credential_id = raw_channel.get("credential_id")
        if raw_credential_id is not None and not isinstance(
            raw_credential_id, str
        ):
            raise ValueError("channel credential_id must be a string")
        channel = EventChannelFilter(
            transport=parse_transport(raw_transport),
            credential_kind=parse_credential_kind(raw_credential_kind),
            credential_id=raw_credential_id,
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
        channel=channel,
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
        "channels": page.channels,
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
