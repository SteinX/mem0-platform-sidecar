import json
import math
from datetime import UTC, datetime
from typing import Any

from mem0_sidecar.core.scope import validate_scope_id
from mem0_sidecar.core.trace_payloads import (
    bounded_trace_document,
    trace_result_summary,
)
from mem0_sidecar.store.models import Event
from mem0_sidecar.store.repositories import EventRepository, _safe_trace_document

DISPLAY_OPERATION = {
    "memory.add": "ADD",
    "memory.search": "SEARCH",
    "memory.list": "GET ALL",
}
_MAX_LEGACY_JSON_BYTES = 65_536
_ENTITY_FIELDS = (
    ("user_id", "user"),
    ("agent_id", "agent"),
    ("app_id", "app"),
    ("run_id", "run"),
)
_TOP_LEVEL_SCOPE_KEYS = frozenset(
    {"projectid", "appid", "userid", "agentid", "runid"}
)
_INTERNAL_KEYS = frozenset(
    {
        "mem0sidecarprojectid",
        "mem0sidecarappid",
        "internalurl",
        "upstreamurl",
        "configuredurl",
    }
)


def _normalized_key(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _safe_json_document(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        return _safe_trace_document(value)
    if len(value) > _MAX_LEGACY_JSON_BYTES:
        return {
            "_trace_truncated": True,
            "reason": "legacy_json_too_large",
        }
    encoded = value.encode("utf-8", "replace")
    if len(encoded) > _MAX_LEGACY_JSON_BYTES:
        return {
            "_trace_truncated": True,
            "reason": "legacy_json_too_large",
        }
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, RecursionError):
        return {"_trace_invalid_json": True}
    return _safe_trace_document(decoded)


def _public_request_value(value: object, *, top_level: bool = False) -> object:
    if isinstance(value, dict):
        public: dict[str, object] = {}
        for key, item in value.items():
            normalized = _normalized_key(key)
            if normalized in _INTERNAL_KEYS:
                continue
            if top_level and normalized in _TOP_LEVEL_SCOPE_KEYS:
                continue
            public[key] = _public_request_value(item)
        return public
    if isinstance(value, list):
        return [_public_request_value(item) for item in value]
    return value


def _public_request(document: dict[str, Any]) -> dict[str, Any]:
    return bounded_trace_document(_public_request_value(document, top_level=True))


def _legacy_scope_value(request: dict[str, Any], field_name: str) -> object:
    direct = request.get(field_name)
    if direct is not None:
        return direct
    if field_name != "app_id":
        return None
    for container_name in ("metadata", "filters"):
        container = request.get(container_name)
        if isinstance(container, dict):
            value = container.get("_mem0_sidecar_app_id")
            if value is not None:
                return value
    return None


def _safe_entity_id(value: object, *, field_name: str) -> str | None:
    try:
        return validate_scope_id(value, field_name=field_name, required=False)
    except ValueError:
        return None


def _event_entities(event: Event, request: dict[str, Any]) -> list[dict[str, str]]:
    entities: list[dict[str, str]] = []
    for field_name, entity_type in _ENTITY_FIELDS:
        canonical = getattr(event, field_name, None)
        candidate = (
            canonical
            if canonical is not None
            else _legacy_scope_value(request, field_name)
        )
        entity_id = _safe_entity_id(candidate, field_name=field_name)
        if entity_id is not None:
            entities.append({"type": entity_type, "id": entity_id})
    return entities


def _safe_nonnegative_int(value: object, *, fallback: int = 0) -> int:
    if type(value) is int and 0 <= value <= 2**63 - 1:
        return value
    return fallback


def _bounded_count_sum(left: int, right: int) -> int:
    return min(left + right, 2**63 - 1)


def _preview_source_count(previews: list[object]) -> int:
    if previews and isinstance(previews[-1], dict):
        marker = previews[-1]
        if set(marker) == {"_trace_truncated_items"}:
            truncated = _safe_nonnegative_int(marker["_trace_truncated_items"])
            if truncated:
                return _bounded_count_sum(len(previews) - 1, truncated)
    return len(previews)


def _safe_latency(value: object) -> float | None:
    if type(value) not in (int, float):
        return None
    latency = float(value)
    return latency if math.isfinite(latency) and latency >= 0 else None


def _utc_timestamp(value: object) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.isoformat().replace("+00:00", "Z")


def _event_status(event: Event) -> str:
    value = getattr(event.status, "value", event.status)
    return value if isinstance(value, str) else "PENDING"


def event_to_trace_dict(event: Event) -> dict[str, Any]:
    """Serialize an event into the bounded public request-trace shape."""

    stored_request = _safe_json_document(getattr(event, "request_json", "{}"))
    response = _safe_json_document(getattr(event, "response_json", "{}"))
    error = _safe_json_document(getattr(event, "error_json", "{}"))
    raw_previews = response.pop("result_previews", [])
    omitted = _safe_nonnegative_int(response.pop("result_previews_omitted", 0))
    if isinstance(raw_previews, list):
        _, previews = trace_result_summary({"results": raw_previews})
        raw_preview_count = _preview_source_count(raw_previews)
        omitted = _bounded_count_sum(
            omitted,
            max(raw_preview_count - len(previews), 0),
        )
    else:
        previews = []
    scan_truncated = response.pop("result_previews_scan_truncated", False) is True
    fallback_result_count = _safe_nonnegative_int(
        response.get("total"), fallback=len(previews)
    )
    result_count = _safe_nonnegative_int(
        getattr(event, "result_count", None),
        fallback=fallback_result_count,
    )
    requested_at = getattr(event, "started_at", None) or getattr(
        event, "created_at", None
    )
    operation = getattr(event, "operation", None)
    if not isinstance(operation, str):
        operation = ""
    correlation_id = getattr(event, "correlation_id", None)
    if not isinstance(correlation_id, str):
        correlation_id = None
    return {
        "id": event.id,
        "correlation_id": correlation_id,
        "operation": operation,
        "display_operation": DISPLAY_OPERATION.get(operation, operation),
        "status": _event_status(event),
        "entities": _event_entities(event, stored_request),
        "request": _public_request(stored_request),
        "response": bounded_trace_document(response),
        "error": bounded_trace_document(error),
        "result_count": result_count,
        "has_results": result_count > 0,
        "latency_ms": _safe_latency(getattr(event, "latency_ms", None)),
        "requested_at": _utc_timestamp(requested_at),
        "completed_at": _utc_timestamp(getattr(event, "completed_at", None)),
        "result_previews": previews,
        "result_previews_omitted": omitted,
        "result_previews_scan_truncated": scan_truncated,
    }


def _event_to_dict(event: Event) -> dict[str, Any]:
    trace = event_to_trace_dict(event)
    return {
        **trace,
        "project_id": event.project_id,
        "subject_type": event.subject_type,
        "subject_id": event.subject_id,
    }


class EventService:
    def __init__(self, events: EventRepository) -> None:
        self.events = events

    def record_successful_mutation(
        self,
        *,
        project_id: str,
        operation: str,
        subject_type: str,
        subject_id: str,
        request: dict[str, Any],
        response: dict[str, Any],
    ) -> Event:
        event = self.events.create_event(
            project_id=project_id,
            operation=operation,
            request=request,
            subject_type=subject_type,
            subject_id=subject_id,
            allow_project_scope=True,
        )
        return self.events.mark_succeeded(event.id, response=response)

    def list_project_events(self, project_id: str) -> list[dict[str, Any]]:
        return [
            _event_to_dict(event)
            for event in self.events.list_project_events(project_id)
        ]

    def get_project_event(
        self,
        project_id: str,
        app_id: str,
        event_id: str,
    ) -> dict[str, Any]:
        return _event_to_dict(
            self.events.get_project_event(project_id, app_id, event_id)
        )
