import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mem0_sidecar.core.explorer_filters import ExplorerFilter, ExplorerQuery
from mem0_sidecar.core.trace_payloads import (
    bounded_trace_document,
    trace_result_summary,
)
from mem0_sidecar.store.models import (
    Category,
    Entity,
    Event,
    EventStatus,
    ExportJob,
    ExportStatus,
    Job,
    JobStatus,
    MemoryIndex,
    Project,
)

_MAX_TRACE_BYTES = 65_536
_MAX_LEGACY_REQUEST_CHARS = 65_536
_MAX_LEGACY_REQUEST_DEPTH = 8
_MAX_RESPONSE_SCAN_FIELDS = 64
_MAX_RESPONSE_ENVELOPE_FIELDS = 40
_MAX_PREVIEW_SCAN_ITEMS = 100
_MAX_CORRELATION_ID_CHARS = 256
_REDACTED_URL = "[REDACTED_URL]"
_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _trace_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _scrub_trace_urls(value: object) -> object:
    if type(value) is str:
        return _URL_PATTERN.sub(_REDACTED_URL, value)
    if type(value) is list:
        return [_scrub_trace_urls(item) for item in value]
    if type(value) is dict:
        scrubbed: dict[str, object] = {}
        for key, item in value.items():
            scrubbed_key = _URL_PATTERN.sub(_REDACTED_URL, key)
            if scrubbed_key in scrubbed:
                scrubbed[scrubbed_key] = {"_trace_key_collision": 2}
                continue
            scrubbed[scrubbed_key] = _scrub_trace_urls(item)
        return scrubbed
    return value


def _safe_trace_document(value: object) -> dict[str, Any]:
    document = bounded_trace_document(value)
    scrubbed = _scrub_trace_urls(document)
    if not isinstance(scrubbed, dict):
        return {}
    serialized_bytes = len(_trace_json(scrubbed).encode("utf-8"))
    if serialized_bytes <= _MAX_TRACE_BYTES:
        return scrubbed
    return {
        "_trace_truncated": True,
        "original_bytes": serialized_bytes,
    }


def _bounded_correlation_id(value: str | None) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise TypeError("correlation_id must be a string")
    character_count = str.__len__(value)
    if character_count <= _MAX_CORRELATION_ID_CHARS and "\x00" not in value:
        return value

    digest = hashlib.sha256()
    for offset in range(0, character_count, 4096):
        chunk = str.__getitem__(value, slice(offset, offset + 4096))
        digest.update(str.encode(chunk, "utf-8", "replace"))
    return f"[SHA256:{digest.hexdigest()}]"


def _bounded_response_envelope(
    response: Mapping[object, object],
) -> dict[object, object]:
    try:
        iterator = (
            dict.__iter__(response)
            if isinstance(response, dict)
            else iter(response)
        )
    except Exception:
        return {"_trace_response_fields_unreadable": True}

    envelope: dict[object, object] = {}
    truncated = False
    for slot in range(_MAX_RESPONSE_SCAN_FIELDS + 1):
        try:
            key = next(iterator)
        except StopIteration:
            break
        except Exception:
            envelope["_trace_response_fields_unreadable"] = True
            break
        if slot == _MAX_RESPONSE_SCAN_FIELDS:
            truncated = True
            break
        if key == "results":
            continue
        if len(envelope) >= _MAX_RESPONSE_ENVELOPE_FIELDS:
            truncated = True
            continue
        try:
            item = (
                dict.__getitem__(response, key)
                if isinstance(response, dict)
                else response[key]
            )
        except Exception:
            item = "[UNREADABLE]"
        envelope[key] = item

    if truncated:
        envelope["_trace_response_fields_truncated"] = True
    return envelope


def _response_results(response: Mapping[str, object]) -> object:
    try:
        if isinstance(response, dict):
            return dict.get(response, "results")
        return response.get("results")
    except Exception:
        return None


def _preview_omission_details(
    response: Mapping[str, object],
    stored_previews: list[dict[str, Any]],
) -> tuple[int, bool]:
    results = _response_results(response)
    if not isinstance(results, list):
        return 0, False
    try:
        returned_count = list.__len__(results)
    except Exception:
        return 0, False

    valid_preview_count = 0
    scan_count = min(returned_count, _MAX_PREVIEW_SCAN_ITEMS)
    for index in range(scan_count):
        try:
            item = list.__getitem__(results, index)
        except Exception:
            break
        _count, preview = trace_result_summary({"results": [item]})
        if preview:
            valid_preview_count += 1

    omitted = max(valid_preview_count - len(stored_previews), 0)
    return omitted, returned_count > _MAX_PREVIEW_SCAN_ITEMS


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class MemoryIndexPage:
    items: list[MemoryIndex]
    total: int
    scan_count: int


@dataclass(frozen=True)
class MemoryClaimResult:
    status: Literal["claimed", "conflict"]
    memory: MemoryIndex | None


@dataclass(frozen=True)
class EventQuery:
    operation: str | None = None
    statuses: tuple[EventStatus, ...] = ()
    has_results: bool | None = None
    from_at: datetime | None = None
    to_at: datetime | None = None
    entity_filters: Mapping[str, str] = field(default_factory=dict)
    page: int = 1
    page_size: int = 50


@dataclass(frozen=True)
class EventPage:
    items: list[Event]
    total: int
    buckets: list[dict[str, object]]


_MEMORY_FILTER_COLUMNS = {
    "user_id": MemoryIndex.user_id,
    "agent_id": MemoryIndex.agent_id,
    "app_id": MemoryIndex.app_id,
    "run_id": MemoryIndex.run_id,
    "memory_id": MemoryIndex.mem0_memory_id,
    "category": MemoryIndex.category,
}

_ENTITY_TYPE_COLUMNS = {
    "user": MemoryIndex.user_id,
    "agent": MemoryIndex.agent_id,
    "app": MemoryIndex.app_id,
    "run": MemoryIndex.run_id,
}


def _scalar_filter_expression(item: ExplorerFilter):
    if item.field == "entity_type":
        if item.operator == "in":
            values = item.value
            return or_(*(_ENTITY_TYPE_COLUMNS[value].is_not(None) for value in values))
        column = _ENTITY_TYPE_COLUMNS[item.value]
        if item.operator == "equals":
            return column.is_not(None)
        return column.is_(None)

    column = _MEMORY_FILTER_COLUMNS[item.field]
    if item.operator == "equals":
        return column == item.value
    if item.operator == "not_equals":
        return column != item.value
    return column.in_(item.value)


def _metadata_projection(memory: MemoryIndex) -> dict[str, Any]:
    try:
        value = json.loads(memory.metadata_projection_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _matches_filter(memory: MemoryIndex, item: ExplorerFilter) -> bool:
    if item.field == "metadata":
        projection = _metadata_projection(memory)
        expected = item.value
        return projection.get(expected["key"]) == expected["value"]

    if item.field == "entity_type":
        if item.operator == "in":
            return any(
                getattr(memory, _ENTITY_TYPE_COLUMNS[value].key) is not None
                for value in item.value
            )
        present = getattr(memory, _ENTITY_TYPE_COLUMNS[item.value].key) is not None
        return present if item.operator == "equals" else not present

    actual = getattr(memory, _MEMORY_FILTER_COLUMNS[item.field].key)
    if item.operator == "equals":
        return actual == item.value
    if item.operator == "not_equals":
        return actual is not None and actual != item.value
    return actual in item.value


def _matches_query_filters(memory: MemoryIndex, query: ExplorerQuery) -> bool:
    matches = (_matches_filter(memory, item) for item in query.filters)
    return all(matches) if query.match == "all" else any(matches)


def _memory_order_by(query: ExplorerQuery):
    if query.sort == "created_at_asc":
        return (MemoryIndex.created_at.asc(), MemoryIndex.mem0_memory_id.asc())
    return (MemoryIndex.created_at.desc(), MemoryIndex.mem0_memory_id.desc())


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _legacy_json_depth_is_bounded(value: str) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for index in range(str.__len__(value)):
        character = str.__getitem__(value, index)
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > _MAX_LEGACY_REQUEST_DEPTH:
                return False
        elif character in "]}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_string


def _event_request(request_json: object) -> dict[str, object] | None:
    if type(request_json) is not str:
        return None
    try:
        character_count = str.__len__(request_json)
    except Exception:
        return None
    if character_count > _MAX_LEGACY_REQUEST_CHARS:
        return None
    if not _legacy_json_depth_is_bounded(request_json):
        return None
    try:
        if len(str.encode(request_json, "utf-8")) > _MAX_TRACE_BYTES:
            return None
    except UnicodeError:
        return None
    try:
        request = json.loads(request_json)
    except (RecursionError, TypeError, UnicodeError, ValueError):
        return None
    return request if type(request) is dict else None


def _request_app_id(request: Mapping[str, object]) -> str | None:
    top_level_values: list[str] = []
    for field_name in ("app_id", "_mem0_sidecar_app_id"):
        if field_name not in request:
            continue
        value = request[field_name]
        if type(value) is not str or not value:
            return None
        top_level_values.append(value)
    if top_level_values:
        return (
            top_level_values[0]
            if all(value == top_level_values[0] for value in top_level_values)
            else None
        )

    metadata = request.get("metadata")
    if type(metadata) is not dict:
        return None
    value = metadata.get("_mem0_sidecar_app_id")
    return value if type(value) is str and value else None


def _matches_event_scope(
    request_json: object,
    app_id: str,
    entity_filters: Mapping[str, str],
) -> bool:
    request = _event_request(request_json)
    if request is None or _request_app_id(request) != app_id:
        return False

    for field_name, expected in entity_filters.items():
        if field_name == "app_id":
            actual = _request_app_id(request)
        elif field_name in {"user_id", "agent_id", "run_id"}:
            actual = request.get(field_name)
        else:
            return False
        if not isinstance(actual, str) or actual != expected:
            return False
    return True


def _event_timeline_buckets(
    created_values: list[datetime],
    query: EventQuery,
) -> list[dict[str, object]]:
    if not created_values:
        return []

    created_values = [_as_utc(created_at) for created_at in created_values]
    range_start = _as_utc(query.from_at) if query.from_at else min(created_values)
    range_end = _as_utc(query.to_at) if query.to_at else max(created_values)
    use_days = range_end - range_start > timedelta(hours=48)

    counts: dict[datetime, int] = {}
    for created_at in created_values:
        bucket = created_at.replace(
            hour=0 if use_days else created_at.hour,
            minute=0,
            second=0,
            microsecond=0,
        )
        counts[bucket] = counts.get(bucket, 0) + 1

    return [
        {
            "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
            "count": counts[timestamp],
        }
        for timestamp in sorted(counts)
    ]


class ProjectRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_default_project(
        self,
        *,
        project_id: str,
        name: str,
        mem0_base_url: str,
        default_user_id: str | None = None,
        default_agent_id: str | None = None,
        default_app_id: str | None = None,
    ) -> Project:
        project = self.session.get(Project, project_id)
        if project is None:
            project = Project(
                id=project_id,
                name=name,
                default_user_id=default_user_id,
                default_app_id=default_app_id or project_id,
                default_agent_id=default_agent_id,
                mem0_base_url=mem0_base_url,
            )
            self.session.add(project)
        else:
            project.name = name
            project.mem0_base_url = mem0_base_url
            if default_user_id is not None:
                project.default_user_id = default_user_id
            if default_agent_id is not None:
                project.default_agent_id = default_agent_id
            if default_app_id is not None:
                project.default_app_id = default_app_id
        self.session.flush()
        return project


class CategoryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_project_category(self, project_id: str, category_id: str) -> Category:
        category = self.session.scalar(
            select(Category).where(
                Category.project_id == project_id, Category.id == category_id
            )
        )
        if category is None:
            raise KeyError(category_id)
        return category

    def find_project_category_by_name(
        self, project_id: str, name: str
    ) -> Category | None:
        return self.session.scalar(
            select(Category).where(
                Category.project_id == project_id, Category.name == name
            )
        )

    def create_project_category(
        self, project_id: str, item: dict[str, Any]
    ) -> Category:
        category = Category(
            project_id=project_id,
            name=str(item["name"]),
            description=str(item.get("description", "")),
            schema_json=_json(item.get("schema", {})),
            enabled=1 if bool(item.get("enabled", True)) else 0,
            strategy=str(item.get("strategy", "metadata")),
        )
        self.session.add(category)
        self.session.flush()
        return category

    def update_project_category(
        self, project_id: str, category_id: str, updates: dict[str, Any]
    ) -> Category:
        category = self.get_project_category(project_id, category_id)
        if "name" in updates:
            category.name = str(updates["name"])
        if "description" in updates:
            category.description = str(updates["description"])
        if "schema" in updates:
            category.schema_json = _json(updates["schema"])
        if "enabled" in updates:
            category.enabled = 1 if bool(updates["enabled"]) else 0
        if "strategy" in updates:
            category.strategy = str(updates["strategy"])
        category.version += 1
        self.session.flush()
        return category

    def delete_project_category(self, project_id: str, category_id: str) -> None:
        category = self.get_project_category(project_id, category_id)
        self.session.delete(category)
        self.session.flush()

    def replace_project_categories(
        self, *, project_id: str, categories: list[dict[str, Any]]
    ) -> list[Category]:
        self.session.execute(delete(Category).where(Category.project_id == project_id))
        self.session.flush()

        created: list[Category] = []
        for item in categories:
            category = Category(
                project_id=project_id,
                name=str(item["name"]),
                description=str(item.get("description", "")),
                schema_json=_json(item.get("schema", {})),
                enabled=1 if bool(item.get("enabled", True)) else 0,
                strategy=str(item.get("strategy", "metadata")),
            )
            self.session.add(category)
            created.append(category)

        self.session.flush()
        return created

    def list_project_categories(self, project_id: str) -> list[Category]:
        return list(
            self.session.scalars(
                select(Category).where(Category.project_id == project_id)
            )
        )


class EventRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_event(
        self,
        *,
        project_id: str,
        operation: str,
        request: dict[str, Any] | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        correlation_id: str | None = None,
    ) -> Event:
        started_at = _utc_now()
        event = Event(
            project_id=project_id,
            operation=operation,
            status=EventStatus.PENDING,
            subject_type=subject_type,
            subject_id=subject_id,
            request_json=_trace_json(_safe_trace_document(request or {})),
            correlation_id=_bounded_correlation_id(correlation_id),
            started_at=started_at,
        )
        self.session.add(event)
        self.session.flush()
        return event

    def get(self, event_id: str) -> Event:
        event = self.session.get(Event, event_id)
        if event is None:
            raise KeyError(event_id)
        return event

    def list_project_events(self, project_id: str) -> list[Event]:
        return list(
            self.session.scalars(
                select(Event)
                .where(Event.project_id == project_id)
                .order_by(Event.created_at, Event.id)
            )
        )

    def get_project_event(self, project_id: str, event_id: str) -> Event:
        event = self.session.scalar(
            select(Event).where(Event.project_id == project_id, Event.id == event_id)
        )
        if event is None:
            raise KeyError(event_id)
        return event

    def mark_succeeded(self, event_id: str, *, response: dict[str, Any]) -> Event:
        event = self.get(event_id)
        result_count, previews = trace_result_summary(response)
        response_document = _bounded_response_envelope(response)
        if previews:
            response_document["result_previews"] = previews
        omitted, scan_truncated = _preview_omission_details(response, previews)
        if omitted:
            response_document["result_previews_omitted"] = omitted
        if scan_truncated:
            response_document["result_previews_scan_truncated"] = True
        event.status = EventStatus.SUCCEEDED
        event.response_json = _trace_json(_safe_trace_document(response_document))
        event.result_count = result_count
        event.has_results = 1 if result_count else 0
        self._complete(event)
        self.session.flush()
        return event

    def mark_failed(self, event_id: str, *, error: dict[str, Any]) -> Event:
        event = self.get(event_id)
        event.status = EventStatus.FAILED
        event.error_json = _trace_json(_safe_trace_document(error))
        event.result_count = 0
        event.has_results = 0
        self._complete(event)
        self.session.flush()
        return event

    def _complete(self, event: Event) -> None:
        completed_at = _utc_now()
        event.completed_at = completed_at
        origin = event.started_at or event.created_at
        if origin is None:
            event.latency_ms = None
            return
        latency = (_as_utc(completed_at) - _as_utc(origin)).total_seconds() * 1000
        event.latency_ms = max(latency, 0.0)

    def query_project_events(
        self,
        project_id: str,
        app_id: str,
        query: EventQuery,
    ) -> EventPage:
        conditions = [Event.project_id == project_id]
        if query.operation is not None:
            conditions.append(Event.operation == query.operation)
        if query.statuses:
            conditions.append(Event.status.in_(query.statuses))
        if query.has_results is not None:
            conditions.append(Event.has_results == (1 if query.has_results else 0))
        if query.from_at is not None:
            conditions.append(Event.created_at >= _as_utc(query.from_at))
        if query.to_at is not None:
            conditions.append(Event.created_at <= _as_utc(query.to_at))

        candidates = list(
            self.session.execute(
                select(Event.id, Event.request_json, Event.created_at)
                .where(*conditions)
                .order_by(Event.created_at.desc(), Event.id.desc())
                .limit(5001)
            )
        )
        if len(candidates) > 5000:
            raise ValueError("entity filter scan exceeds 5000 records")

        matches = [
            (event_id, created_at)
            for event_id, request_json, created_at in candidates
            if _matches_event_scope(request_json, app_id, query.entity_filters)
        ]
        offset = (query.page - 1) * query.page_size
        page_ids = [
            event_id
            for event_id, _created_at in matches[
                offset : offset + query.page_size
            ]
        ]
        loaded_events = (
            {
                event.id: event
                for event in self.session.scalars(
                    select(Event).where(
                        Event.project_id == project_id,
                        Event.id.in_(page_ids),
                    )
                )
            }
            if page_ids
            else {}
        )
        return EventPage(
            items=[
                loaded_events[event_id]
                for event_id in page_ids
                if event_id in loaded_events
            ],
            total=len(matches),
            buckets=_event_timeline_buckets(
                [created_at for _event_id, created_at in matches],
                query,
            ),
        )


class MemoryIndexRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_memory(
        self,
        *,
        project_id: str,
        mem0_memory_id: str,
        app_id: str | None = None,
        include_deleted: bool = False,
    ) -> MemoryIndex | None:
        statement = select(MemoryIndex).where(
            MemoryIndex.project_id == project_id,
            MemoryIndex.mem0_memory_id == mem0_memory_id,
        )
        if app_id is not None:
            statement = statement.where(MemoryIndex.app_id == app_id)
        if not include_deleted:
            statement = statement.where(MemoryIndex.deleted_at.is_(None))
        return self.session.scalar(statement)

    def list_scoped_memory_ids(
        self,
        *,
        project_id: str,
        mem0_memory_ids: list[str],
        user_id: str | None,
        app_id: str | None,
        agent_id: str | None,
        run_id: str | None,
    ) -> set[str]:
        if not mem0_memory_ids:
            return set()

        statement = select(MemoryIndex.mem0_memory_id).where(
            MemoryIndex.project_id == project_id,
            MemoryIndex.deleted_at.is_(None),
            MemoryIndex.mem0_memory_id.in_(mem0_memory_ids),
        )
        if user_id is not None:
            statement = statement.where(MemoryIndex.user_id == user_id)
        if app_id is not None:
            statement = statement.where(MemoryIndex.app_id == app_id)
        if agent_id is not None:
            statement = statement.where(MemoryIndex.agent_id == agent_id)
        if run_id is not None:
            statement = statement.where(MemoryIndex.run_id == run_id)

        return set(self.session.scalars(statement))

    def list_export_candidates(
        self,
        *,
        project_id: str,
        filters: dict[str, Any],
    ) -> list[MemoryIndex]:
        statement = (
            select(MemoryIndex)
            .where(
                MemoryIndex.project_id == project_id,
                MemoryIndex.deleted_at.is_(None),
            )
            .order_by(MemoryIndex.created_at, MemoryIndex.mem0_memory_id)
        )
        if (user_id := filters.get("user_id")) is not None:
            statement = statement.where(MemoryIndex.user_id == user_id)
        if (app_id := filters.get("app_id")) is not None:
            statement = statement.where(MemoryIndex.app_id == app_id)
        if (agent_id := filters.get("agent_id")) is not None:
            statement = statement.where(MemoryIndex.agent_id == agent_id)
        if (run_id := filters.get("run_id")) is not None:
            statement = statement.where(MemoryIndex.run_id == run_id)
        return list(self.session.scalars(statement))

    def query_project_memories(
        self,
        project_id: str,
        app_id: str,
        query: ExplorerQuery,
    ) -> MemoryIndexPage:
        scope_conditions = [
            MemoryIndex.project_id == project_id,
            MemoryIndex.app_id == app_id,
            MemoryIndex.deleted_at.is_(None),
        ]
        if query.date_range.from_at is not None:
            scope_conditions.append(MemoryIndex.created_at >= query.date_range.from_at)
        if query.date_range.to_at is not None:
            scope_conditions.append(MemoryIndex.created_at <= query.date_range.to_at)

        scalar_filters = [item for item in query.filters if item.field != "metadata"]
        metadata_filters = [item for item in query.filters if item.field == "metadata"]
        scalar_expressions = [
            _scalar_filter_expression(item) for item in scalar_filters
        ]

        if not metadata_filters:
            conditions = list(scope_conditions)
            if scalar_expressions:
                combine = and_ if query.match == "all" else or_
                conditions.append(combine(*scalar_expressions))

            total = self.session.scalar(
                select(func.count()).select_from(MemoryIndex).where(*conditions)
            )
            offset = (query.page - 1) * query.page_size
            statement = (
                select(MemoryIndex)
                .where(*conditions)
                .order_by(*_memory_order_by(query))
                .offset(offset)
                .limit(query.page_size)
            )
            return MemoryIndexPage(
                items=list(self.session.scalars(statement)),
                total=total or 0,
                scan_count=0,
            )

        candidate_conditions = list(scope_conditions)
        if query.match == "all" and scalar_expressions:
            candidate_conditions.append(and_(*scalar_expressions))

        scan_count = self.session.scalar(
            select(func.count())
            .select_from(MemoryIndex)
            .where(*candidate_conditions)
        ) or 0
        if scan_count > 5000:
            raise ValueError("metadata filter scan exceeds 5000 records")

        candidates = list(
            self.session.scalars(
                select(MemoryIndex)
                .where(*candidate_conditions)
                .order_by(*_memory_order_by(query))
            )
        )
        matches = [
            memory
            for memory in candidates
            if _matches_query_filters(memory, query)
        ]
        offset = (query.page - 1) * query.page_size
        return MemoryIndexPage(
            items=matches[offset : offset + query.page_size],
            total=len(matches),
            scan_count=scan_count,
        )

    def mark_stale(
        self,
        project_id: str,
        mem0_memory_ids: Iterable[str],
    ) -> int:
        memory_ids = set(mem0_memory_ids)
        if not memory_ids:
            return 0

        memories = list(
            self.session.scalars(
                select(MemoryIndex).where(
                    MemoryIndex.project_id == project_id,
                    MemoryIndex.mem0_memory_id.in_(memory_ids),
                    MemoryIndex.deleted_at.is_(None),
                )
            )
        )
        stale_at = _utc_now()
        for memory in memories:
            memory.deleted_at = stale_at
        self.session.flush()
        return len(memories)

    def mark_stale_if_unchanged(
        self,
        *,
        project_id: str,
        app_id: str,
        mem0_memory_ids: Iterable[str],
        updated_at_lte: datetime,
    ) -> int:
        memory_ids = set(mem0_memory_ids)
        if not memory_ids:
            return 0

        result = self.session.execute(
            update(MemoryIndex)
            .where(
                MemoryIndex.project_id == project_id,
                MemoryIndex.app_id == app_id,
                MemoryIndex.mem0_memory_id.in_(memory_ids),
                MemoryIndex.deleted_at.is_(None),
                MemoryIndex.updated_at <= updated_at_lte,
            )
            .values(deleted_at=_utc_now())
            .execution_options(synchronize_session="fetch")
        )
        self.session.flush()
        return result.rowcount or 0

    def upsert_memory(
        self,
        *,
        project_id: str,
        mem0_memory_id: str,
        user_id: str | None,
        app_id: str | None,
        category: str | None,
        agent_id: str | None = None,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryIndex:
        projection_now = _utc_now()
        memory = self.session.scalar(
            select(MemoryIndex).where(
                MemoryIndex.project_id == project_id,
                MemoryIndex.mem0_memory_id == mem0_memory_id,
            )
        )
        if memory is None:
            memory = MemoryIndex(project_id=project_id, mem0_memory_id=mem0_memory_id)
            self.session.add(memory)

        memory.user_id = user_id
        memory.agent_id = agent_id
        memory.app_id = app_id
        memory.run_id = run_id
        memory.category = category
        memory.metadata_projection_json = _json(metadata or {})
        memory.deleted_at = None
        memory.updated_at = projection_now
        self.session.flush()
        return memory

    def claim_memory(
        self,
        *,
        project_id: str,
        mem0_memory_id: str,
        user_id: str | None,
        app_id: str,
        category: str | None,
        agent_id: str | None = None,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryClaimResult:
        projection_now = _utc_now()
        values = {
            "user_id": user_id,
            "agent_id": agent_id,
            "app_id": app_id,
            "run_id": run_id,
            "category": category,
            "metadata_projection_json": _json(metadata or {}),
            "deleted_at": None,
            "updated_at": projection_now,
        }

        def update_claimable() -> int:
            result = self.session.execute(
                update(MemoryIndex)
                .where(
                    MemoryIndex.project_id == project_id,
                    MemoryIndex.mem0_memory_id == mem0_memory_id,
                    or_(
                        MemoryIndex.app_id == app_id,
                        MemoryIndex.deleted_at.is_not(None),
                    ),
                )
                .values(**values)
                .execution_options(synchronize_session="fetch")
            )
            return result.rowcount or 0

        def claimed_result() -> MemoryClaimResult:
            memory = self.session.scalar(
                select(MemoryIndex).where(
                    MemoryIndex.project_id == project_id,
                    MemoryIndex.mem0_memory_id == mem0_memory_id,
                )
            )
            if memory is None:
                raise RuntimeError("Claimed memory projection could not be loaded")
            return MemoryClaimResult(status="claimed", memory=memory)

        if update_claimable():
            self.session.flush()
            return claimed_result()

        existing_id = self.session.scalar(
            select(MemoryIndex.id).where(
                MemoryIndex.project_id == project_id,
                MemoryIndex.mem0_memory_id == mem0_memory_id,
            )
        )
        if existing_id is not None:
            return MemoryClaimResult(status="conflict", memory=None)

        try:
            with self.session.begin_nested():
                memory = MemoryIndex(
                    project_id=project_id,
                    mem0_memory_id=mem0_memory_id,
                    **values,
                )
                self.session.add(memory)
                self.session.flush()
        except IntegrityError:
            if update_claimable():
                self.session.flush()
                return claimed_result()
            return MemoryClaimResult(status="conflict", memory=None)

        return MemoryClaimResult(status="claimed", memory=memory)

    def delete_memory(
        self,
        *,
        project_id: str,
        mem0_memory_id: str,
    ) -> MemoryIndex | None:
        memory = self.get_memory(project_id=project_id, mem0_memory_id=mem0_memory_id)
        if memory is None:
            return None

        memory.deleted_at = _utc_now()
        self.session.flush()
        return memory


class EntityRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_entity(
        self,
        *,
        project_id: str,
        entity_type: str,
        entity_id: str,
        display_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Entity:
        entity = self.session.scalar(
            select(Entity).where(
                Entity.project_id == project_id,
                Entity.entity_type == entity_type,
                Entity.entity_id == entity_id,
            )
        )
        if entity is None:
            entity = Entity(
                project_id=project_id,
                entity_type=entity_type,
                entity_id=entity_id,
            )
            self.session.add(entity)

        entity.display_name = display_name
        entity.metadata_json = _json(metadata or {})
        entity.last_seen_at = _utc_now()
        self.session.flush()
        return entity


class ExportJobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        project_id: str,
        export_format: str,
        filters: dict[str, Any],
    ) -> ExportJob:
        job = ExportJob(
            project_id=project_id,
            format=export_format,
            filters_json=_json(filters),
            status=ExportStatus.PENDING,
        )
        self.session.add(job)
        self.session.flush()
        return job

    def get(self, project_id: str, job_id: str) -> ExportJob:
        job = self.session.scalar(
            select(ExportJob).where(
                ExportJob.project_id == project_id,
                ExportJob.id == job_id,
            )
        )
        if job is None:
            raise KeyError(job_id)
        return job

    def list_project_exports(self, project_id: str) -> list[ExportJob]:
        return list(
            self.session.scalars(
                select(ExportJob)
                .where(ExportJob.project_id == project_id)
                .order_by(ExportJob.created_at.desc(), ExportJob.id.desc())
            )
        )

    def mark_running(self, project_id: str, job_id: str) -> ExportJob:
        job = self.get(project_id, job_id)
        job.status = ExportStatus.RUNNING
        job.started_at = _utc_now()
        self.session.flush()
        return job

    def mark_succeeded(
        self,
        project_id: str,
        job_id: str,
        *,
        result: dict[str, Any],
        total_count: int,
        exported_count: int,
        skipped_count: int,
    ) -> ExportJob:
        job = self.get(project_id, job_id)
        job.status = ExportStatus.SUCCEEDED
        job.result_json = _json(result)
        job.error_json = _json({})
        job.total_count = total_count
        job.exported_count = exported_count
        job.skipped_count = skipped_count
        job.completed_at = _utc_now()
        self.session.flush()
        return job

    def mark_failed(
        self, project_id: str, job_id: str, *, error: dict[str, Any]
    ) -> ExportJob:
        job = self.get(project_id, job_id)
        job.status = ExportStatus.FAILED
        job.error_json = _json(error)
        job.completed_at = _utc_now()
        self.session.flush()
        return job


class JobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def enqueue(
        self,
        *,
        project_id: str,
        event_id: str | None,
        job_type: str,
        payload: dict[str, Any],
    ) -> Job:
        job = Job(
            project_id=project_id,
            event_id=event_id,
            job_type=job_type,
            payload_json=_json(payload),
        )
        self.session.add(job)
        self.session.flush()
        return job

    def claim_next(self) -> Job | None:
        job = self.session.scalar(
            select(Job)
            .where(Job.status == JobStatus.PENDING)
            .order_by(Job.created_at)
        )
        if job is None:
            return None

        job.status = JobStatus.RUNNING
        job.locked_at = _utc_now()
        job.attempt_count += 1
        self.session.flush()
        return job
