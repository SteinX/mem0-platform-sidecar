import hashlib
import ipaddress
import json
import re
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import parse_qsl, unquote, urlsplit

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mem0_sidecar.core.consolidation_policy import ConsolidationPolicySpec
from mem0_sidecar.core.explorer_filters import (
    EXPLORER_RECORD_HORIZON,
    ExplorerFilter,
    ExplorerQuery,
)
from mem0_sidecar.core.scope import validate_scope_id
from mem0_sidecar.core.trace_payloads import (
    bounded_trace_document,
    sanitize_trace_payload,
    trace_key_is_secret,
    trace_result_summary,
)
from mem0_sidecar.store.models import (
    Category,
    ConsolidationLineage,
    ConsolidationPolicy,
    ConsolidationProposal,
    ConsolidationRun,
    Entity,
    Event,
    EventStatus,
    ExportJob,
    ExportStatus,
    Job,
    JobStatus,
    MemoryIndex,
    MutationIntent,
    MutationIntentTarget,
    Project,
)

_MAX_TRACE_BYTES = 65_536
_MAX_LEGACY_REQUEST_CHARS = 65_536
_MAX_LEGACY_REQUEST_DEPTH = 9
_MAX_RESPONSE_SCAN_FIELDS = 64
_MAX_RESPONSE_ENVELOPE_FIELDS = 40
_MAX_PREVIEW_SCAN_ITEMS = 100
_MAX_CORRELATION_ID_CHARS = 256
EVENT_SCAN_LIMIT = 5000
ENTITY_REFRESH_IDENTITY_LIMIT = 800
_EVENT_QUERY_MAX_ATTEMPTS = 2
_EVENT_QUERY_UNSTABLE_ERROR = (
    "event query snapshot changed; retry with narrower filters"
)
_REDACTED_URL = "[REDACTED_URL]"
_URL_PATTERN = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
_URL_TRAILING_PUNCTUATION = "),.;"
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_AUTHORIZATION_VALUE_PATTERN = re.compile(
    r"(?im)(?P<prefix>(?<![A-Za-z0-9])(?P<quote>['\"]?)"
    r"(?:proxy[ ._-]?)?"
    r"authorization(?P=quote)\s*[:=]\s*)"
    r"(?P<value>[^\r\n]+(?:\r?\n[ \t]+[^\r\n]*)*)"
)
_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?P<prefix>(?P<quote>['\"]?)(?P<key>[a-z][a-z0-9_. -]{0,127}?)"
    r"(?P=quote)\s*[:=]\s*)"
    r"(?P<value>\[REDACTED\]|\"(?:\\.|[^\"\\])*\"|"
    r"'(?:\\.|[^'\\])*'|[^\s,;)\]}]+)"
)
_STANDALONE_CREDENTIAL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"sk[-_][A-Za-z0-9_-]{6,}|"
    r"gh[pousr]_[A-Za-z0-9_-]{8,}|"
    r"github_pat_[A-Za-z0-9_-]{6,}|"
    r"xox[baprs]-[A-Za-z0-9_-]{6,}|"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}"
    r")(?![A-Za-z0-9])"
)
_RESPONSE_STRING_FIELDS = frozenset({"id", "memory", "memory_id", "message", "status"})
_RESPONSE_INTEGER_FIELDS = frozenset({"count", "total"})
_RESPONSE_BOOLEAN_FIELDS = frozenset({"created", "deleted", "ok", "success", "updated"})
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


def _normalized_url_host(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        _port = parsed.port
    except (TypeError, UnicodeError, ValueError):
        return None
    if not host:
        return None
    return host.lower().rstrip(".")


def _valid_public_domain(host: str) -> bool:
    labels = host.split(".")
    if len(labels) < 2:
        return False
    for label in labels:
        try:
            ascii_label = label.encode("idna").decode("ascii")
        except UnicodeError:
            return False
        if (
            not ascii_label
            or len(ascii_label) > 63
            or ascii_label.startswith("-")
            or ascii_label.endswith("-")
            or re.fullmatch(r"[a-zA-Z0-9-]+", ascii_label) is None
        ):
            return False
    return not labels[-1].isdigit()


def _sensitive_url(url: str, internal_hosts: frozenset[str]) -> bool:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        _port = parsed.port
    except (TypeError, UnicodeError, ValueError):
        return True
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        return True
    if parsed.username is not None or parsed.password is not None:
        return True
    if _INVALID_PERCENT_ESCAPE.search(url) is not None:
        return True

    normalized_host = host.lower().rstrip(".")
    if normalized_host in internal_hosts:
        return True
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        if normalized_host.endswith(
            (".internal", ".local", ".localhost")
        ) or not _valid_public_domain(normalized_host):
            return True
    else:
        if not address.is_global:
            return True

    def fully_decode(component: str) -> str:
        current = component
        for _attempt in range(3):
            decoded = unquote(current, encoding="utf-8", errors="strict")
            if decoded == current:
                if _INVALID_PERCENT_ESCAPE.search(decoded) is not None:
                    raise ValueError("ambiguous percent escape")
                return decoded
            current = decoded
        raise ValueError("excessive percent encoding")

    try:
        decoded_query = fully_decode(parsed.query)
        decoded_fragment = fully_decode(parsed.fragment)
    except (UnicodeError, ValueError):
        return True
    components = [decoded_query]
    if "=" in decoded_fragment or "&" in decoded_fragment:
        components.append(decoded_fragment)
    for component in components:
        if not component:
            continue
        try:
            items = parse_qsl(
                component,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=64,
            )
        except (UnicodeError, ValueError):
            return True
        if any(trace_key_is_secret(key) for key, _value in items):
            return True
    return False


def _scrub_url_string(value: str, internal_hosts: frozenset[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        matched = match.group(0)
        core = matched
        suffix = ""
        while core and core[-1] in _URL_TRAILING_PUNCTUATION:
            suffix = core[-1] + suffix
            core = core[:-1]
        if not core or _sensitive_url(core, internal_hosts):
            return _REDACTED_URL + suffix
        return core + suffix

    return _URL_PATTERN.sub(replace, value)


def _redacted_assignment_value(value: str) -> str:
    if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
        return f"{value[0]}[REDACTED]{value[-1]}"
    suffix = "." if value.endswith(".") else ""
    return "[REDACTED]" + suffix


def _scrub_credential_string(value: str) -> str:
    scrubbed = _AUTHORIZATION_VALUE_PATTERN.sub(
        lambda match: match.group("prefix")
        + _redacted_assignment_value(match.group("value")),
        value,
    )

    def replace_assignment(match: re.Match[str]) -> str:
        if not trace_key_is_secret(match.group("key")):
            return match.group(0)
        return match.group("prefix") + _redacted_assignment_value(
            match.group("value")
        )

    scrubbed = _CREDENTIAL_ASSIGNMENT_PATTERN.sub(replace_assignment, scrubbed)
    return _STANDALONE_CREDENTIAL_PATTERN.sub("[REDACTED]", scrubbed)


def _scrub_trace_string(value: str, internal_hosts: frozenset[str]) -> str:
    stripped = value.strip()
    if stripped.startswith(("{", "[")) and stripped.endswith(("}", "]")):
        try:
            parsed = json.loads(stripped)
        except (RecursionError, TypeError, UnicodeError, ValueError):
            parsed = None
        if type(parsed) in {dict, list}:
            sanitized = sanitize_trace_payload(parsed)
            scrubbed = _scrub_trace_strings(sanitized, internal_hosts)
            return _trace_json(scrubbed)
    return _scrub_credential_string(_scrub_url_string(value, internal_hosts))


def _scrub_trace_strings(
    value: object,
    internal_hosts: frozenset[str],
) -> object:
    if type(value) is str:
        return _scrub_trace_string(value, internal_hosts)
    if type(value) is list:
        return [_scrub_trace_strings(item, internal_hosts) for item in value]
    if type(value) is dict:
        scrubbed: dict[str, object] = {}
        for key, item in value.items():
            scrubbed_key = _scrub_trace_string(key, internal_hosts)
            if scrubbed_key in scrubbed:
                scrubbed[scrubbed_key] = {"_trace_key_collision": 2}
                continue
            scrubbed[scrubbed_key] = _scrub_trace_strings(item, internal_hosts)
        return scrubbed
    return value


def _safe_trace_document(
    value: object,
    *,
    internal_hosts: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    document = bounded_trace_document(value)
    scrubbed = _scrub_trace_strings(document, internal_hosts)
    if not isinstance(scrubbed, dict):
        return {}
    serialized_bytes = len(_trace_json(scrubbed).encode("utf-8"))
    if serialized_bytes <= _MAX_TRACE_BYTES:
        return scrubbed
    return {
        "_trace_truncated": True,
        "original_bytes": serialized_bytes,
    }


def _bounded_correlation_id(
    value: str | None,
    *,
    internal_hosts: frozenset[str],
) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise TypeError("correlation_id must be a string")
    character_count = str.__len__(value)
    contains_sensitive_value = _scrub_trace_string(value, internal_hosts) != value
    if (
        character_count <= _MAX_CORRELATION_ID_CHARS
        and "\x00" not in value
        and not contains_sensitive_value
    ):
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
            dict.__iter__(response) if isinstance(response, dict) else iter(response)
        )
    except Exception:
        return {"_trace_response_fields_unreadable": True}

    observed_keys: list[object] = []
    for slot in range(_MAX_RESPONSE_SCAN_FIELDS + 1):
        try:
            key = next(iterator)
        except StopIteration:
            break
        except Exception:
            return {"_trace_response_fields_unreadable": True}
        if slot == _MAX_RESPONSE_SCAN_FIELDS:
            return {"_trace_response_envelope_truncated": True}
        observed_keys.append(key)

    allowed_keys = sorted(
        key
        for key in observed_keys
        if type(key) is str
        and key
        in (
            _RESPONSE_STRING_FIELDS
            | _RESPONSE_INTEGER_FIELDS
            | _RESPONSE_BOOLEAN_FIELDS
        )
    )[:_MAX_RESPONSE_ENVELOPE_FIELDS]
    envelope: dict[object, object] = {}
    for key in allowed_keys:
        try:
            item = (
                dict.__getitem__(response, key)
                if isinstance(response, dict)
                else response[key]
            )
        except Exception:
            continue
        if key in _RESPONSE_STRING_FIELDS and type(item) is str:
            envelope[key] = item
        elif key in _RESPONSE_INTEGER_FIELDS and type(item) is int:
            envelope[key] = item
        elif key in _RESPONSE_BOOLEAN_FIELDS and type(item) is bool:
            envelope[key] = item
    return envelope


def _response_value(response: Mapping[str, object], key: str) -> object:
    try:
        if isinstance(response, dict):
            return dict.get(response, key)
        return response.get(key)
    except Exception:
        return None


def _preview_omission_details(
    response: Mapping[str, object],
    stored_previews: list[dict[str, Any]],
) -> tuple[int, bool]:
    results = _response_value(response, "results")
    if not isinstance(results, list):
        return 0, False
    try:
        returned_count = list.__len__(results)
    except Exception:
        return 0, False

    omitted = max(returned_count - len(stored_previews), 0)
    return omitted, returned_count > _MAX_PREVIEW_SCAN_ITEMS


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _project_internal_hosts(session: Session, project_id: str) -> frozenset[str]:
    project = session.get(Project, project_id)
    if project is None:
        return frozenset()
    host = _normalized_url_host(project.mem0_base_url)
    return frozenset({host}) if host is not None else frozenset()


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


@dataclass(frozen=True)
class _EventCandidate:
    event_id: str
    app_id: str | None
    user_id: str | None
    agent_id: str | None
    run_id: str | None
    request_json: str
    created_at: datetime
    operation: str
    status: EventStatus
    has_results: int
    result_count: int
    correlation_id: str | None
    latency_ms: float | None
    started_at: datetime | None
    completed_at: datetime | None
    subject_type: str | None
    subject_id: str | None


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
    values: list[str] = []
    for field_name in ("app_id", "_mem0_sidecar_app_id"):
        if field_name not in request:
            continue
        value = request[field_name]
        try:
            validated = validate_scope_id(value, field_name="app_id")
        except ValueError:
            return None
        values.append(validated)

    metadata = request.get("metadata")
    if type(metadata) is dict and "_mem0_sidecar_app_id" in metadata:
        value = metadata["_mem0_sidecar_app_id"]
        try:
            validated = validate_scope_id(value, field_name="app_id")
        except ValueError:
            return None
        values.append(validated)
    if not values:
        return None
    return values[0] if all(value == values[0] for value in values) else None


def _raw_canonical_event_app_id(
    request: dict[str, Any],
    explicit_app_id: str | None,
) -> str | None:
    values: list[object] = []
    for field_name in ("app_id", "_mem0_sidecar_app_id"):
        if field_name in request:
            values.append(request[field_name])
    metadata = request.get("metadata")
    if type(metadata) is dict and "_mem0_sidecar_app_id" in metadata:
        values.append(metadata["_mem0_sidecar_app_id"])
    if explicit_app_id is not None:
        values.append(explicit_app_id)
    if not values:
        return None

    canonical: list[str] = []
    for value in values:
        try:
            validated = validate_scope_id(value, field_name="app_id")
        except ValueError as exc:
            raise ValueError("canonical event app scope is invalid") from exc
        canonical.append(validated)
    if any(value != canonical[0] for value in canonical[1:]):
        raise ValueError("canonical event app scope markers conflict")
    return canonical[0]


def _raw_canonical_event_entity_id(
    request: dict[str, Any],
    explicit_value: str | None,
    *,
    field_name: Literal["user_id", "agent_id", "run_id"],
) -> str | None:
    values: list[object] = []
    if field_name in request:
        values.append(request[field_name])
    if explicit_value is not None:
        values.append(explicit_value)
    if not values:
        return None

    canonical: list[str] = []
    for value in values:
        try:
            validated = validate_scope_id(value, field_name=field_name)
        except ValueError as exc:
            entity_name = field_name.removesuffix("_id")
            raise ValueError(f"canonical event {entity_name} scope is invalid") from exc
        canonical.append(validated)
    if any(value != canonical[0] for value in canonical[1:]):
        entity_name = field_name.removesuffix("_id")
        raise ValueError(f"canonical event {entity_name} scope markers conflict")
    return canonical[0]


def _legacy_request_entity_id(
    request: Mapping[str, object],
    field_name: Literal["user_id", "agent_id", "run_id"],
) -> str | None:
    if field_name not in request:
        return None
    try:
        return validate_scope_id(request[field_name], field_name=field_name)
    except ValueError:
        return None


def _same_optional_datetime(
    left: datetime | None,
    right: datetime | None,
) -> bool:
    if left is None or right is None:
        return left is right
    return _as_utc(left) == _as_utc(right)


def _event_matches_candidate(event: object, candidate: _EventCandidate) -> bool:
    if not isinstance(event, Event):
        return False
    return (
        event.id == candidate.event_id
        and event.app_id == candidate.app_id
        and event.user_id == candidate.user_id
        and event.agent_id == candidate.agent_id
        and event.run_id == candidate.run_id
        and event.request_json == candidate.request_json
        and _as_utc(event.created_at) == _as_utc(candidate.created_at)
        and event.operation == candidate.operation
        and event.status == candidate.status
        and event.has_results == candidate.has_results
        and event.result_count == candidate.result_count
        and event.correlation_id == candidate.correlation_id
        and event.latency_ms == candidate.latency_ms
        and _same_optional_datetime(event.started_at, candidate.started_at)
        and _same_optional_datetime(event.completed_at, candidate.completed_at)
        and event.subject_type == candidate.subject_type
        and event.subject_id == candidate.subject_id
    )


def _matches_event_scope(
    request_json: object,
    app_id: str,
    entity_filters: Mapping[str, str],
    canonical_app_id: str | None,
    canonical_user_id: str | None,
    canonical_agent_id: str | None,
    canonical_run_id: str | None,
) -> bool:
    effective_app_id = canonical_app_id
    request: dict[str, object] | None = None
    if effective_app_id is None:
        request = _event_request(request_json)
        if request is None:
            return False
        effective_app_id = _request_app_id(request)
    if effective_app_id != app_id:
        return False

    canonical_entities = {
        "user_id": canonical_user_id,
        "agent_id": canonical_agent_id,
        "run_id": canonical_run_id,
    }
    for field_name, expected in entity_filters.items():
        if field_name == "app_id":
            actual = effective_app_id
        elif field_name in {"user_id", "agent_id", "run_id"}:
            actual = canonical_entities[field_name]
            if actual is None:
                if request is None:
                    request = _event_request(request_json)
                if request is None:
                    return False
                actual = _legacy_request_entity_id(request, field_name)
        else:
            return False
        if actual != expected:
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

    def lock_for_mutation(self, project_id: str) -> Project:
        """Serialize projection mutations in Project -> MemoryIndex -> Entity order."""

        if self.session.get_bind().dialect.name == "sqlite":
            result = self.session.execute(
                update(Project)
                .where(Project.id == project_id)
                .values(updated_at=Project.updated_at)
                .execution_options(synchronize_session=False)
            )
            if not result.rowcount:
                raise KeyError(project_id)
            project = self.session.get(Project, project_id)
            if project is None:
                raise KeyError(project_id)
            return project

        with self.session.no_autoflush:
            project = self.session.scalar(
                select(Project)
                .where(Project.id == project_id)
                .with_for_update()
            )
        if project is None:
            raise KeyError(project_id)
        return project


class ServiceCapabilityRepository:
    _BRIDGE_KEY = "bridge_routing"
    _FRESH_FOR = timedelta(minutes=10)
    _MAX_INSTANCES = 32

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _settings(project: Project) -> dict[str, Any]:
        try:
            value = json.loads(project.settings_json or "{}")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def record_bridge_heartbeat(
        self,
        *,
        project_id: str,
        instance_id: str,
        bridge_version: str,
        routes_reads: bool,
        routes_writes: bool,
        observed_at: datetime | None = None,
    ) -> dict[str, object]:
        validate_scope_id(instance_id, field_name="instance_id")
        if not isinstance(bridge_version, str) or not 1 <= len(bridge_version) <= 128:
            raise ValueError("bridge_version must contain 1-128 characters")
        if type(routes_reads) is not bool or type(routes_writes) is not bool:
            raise ValueError("bridge routing capabilities must be booleans")
        observed_at = observed_at or _utc_now()
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        else:
            observed_at = observed_at.astimezone(UTC)

        project = ProjectRepository(self.session).lock_for_mutation(project_id)
        settings = self._settings(project)
        capabilities = settings.get("service_capabilities")
        if not isinstance(capabilities, dict):
            capabilities = {}
        bridge = capabilities.get(self._BRIDGE_KEY)
        if not isinstance(bridge, dict):
            bridge = {}
        instances = bridge.get("instances")
        if not isinstance(instances, dict):
            instances = {}
        instances[instance_id] = {
            "bridge_version": bridge_version,
            "routes_reads": routes_reads,
            "routes_writes": routes_writes,
            "last_seen_at": observed_at.isoformat(),
        }
        ordered_instances = sorted(
            instances.items(),
            key=lambda item: str(
                item[1].get("last_seen_at", "")
                if isinstance(item[1], dict)
                else ""
            ),
            reverse=True,
        )[: self._MAX_INSTANCES]
        bridge["instances"] = dict(ordered_instances)
        capabilities[self._BRIDGE_KEY] = bridge
        settings["service_capabilities"] = capabilities
        project.settings_json = _json(settings)
        self.session.flush()
        return self.bridge_routing_status(project_id, now=observed_at)

    def bridge_routing_status(
        self,
        project_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, object]:
        project = self.session.get(Project, project_id)
        if project is None:
            raise KeyError(project_id)
        now = now or _utc_now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        else:
            now = now.astimezone(UTC)
        settings = self._settings(project)
        capabilities = settings.get("service_capabilities")
        bridge = (
            capabilities.get(self._BRIDGE_KEY)
            if isinstance(capabilities, dict)
            else None
        )
        instances = bridge.get("instances") if isinstance(bridge, dict) else None
        ready_instances: list[tuple[str, datetime]] = []
        if isinstance(instances, dict):
            for instance_id, raw in instances.items():
                if not isinstance(instance_id, str) or not isinstance(raw, dict):
                    continue
                raw_seen = raw.get("last_seen_at")
                if not isinstance(raw_seen, str):
                    continue
                try:
                    seen = datetime.fromisoformat(raw_seen.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if seen.tzinfo is None:
                    seen = seen.replace(tzinfo=UTC)
                else:
                    seen = seen.astimezone(UTC)
                if (
                    raw.get("routes_reads") is True
                    and raw.get("routes_writes") is True
                    and seen >= now - self._FRESH_FOR
                    and seen <= now + timedelta(minutes=1)
                ):
                    ready_instances.append((instance_id, seen))
        last_seen = max((seen for _instance, seen in ready_instances), default=None)
        return {
            "ready": bool(ready_instances),
            "ready_instance_count": len(ready_instances),
            "last_seen_at": last_seen,
        }

    def bridge_routing_ready(
        self,
        project_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        return bool(self.bridge_routing_status(project_id, now=now)["ready"])


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
        app_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        run_id: str | None = None,
        operation: str,
        request: dict[str, Any] | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        correlation_id: str | None = None,
        allow_project_scope: bool = False,
    ) -> Event:
        started_at = _utc_now()
        internal_hosts = _project_internal_hosts(self.session, project_id)
        raw_request = request or {}
        canonical_app_id = _raw_canonical_event_app_id(raw_request, app_id)
        if canonical_app_id is None and not allow_project_scope:
            raise ValueError("canonical event app scope is required")
        canonical_user_id = _raw_canonical_event_entity_id(
            raw_request, user_id, field_name="user_id"
        )
        canonical_agent_id = _raw_canonical_event_entity_id(
            raw_request, agent_id, field_name="agent_id"
        )
        canonical_run_id = _raw_canonical_event_entity_id(
            raw_request, run_id, field_name="run_id"
        )
        event = Event(
            project_id=project_id,
            app_id=canonical_app_id,
            user_id=canonical_user_id,
            agent_id=canonical_agent_id,
            run_id=canonical_run_id,
            operation=operation,
            status=EventStatus.PENDING,
            subject_type=subject_type,
            subject_id=subject_id,
            request_json=_trace_json(
                _safe_trace_document(raw_request, internal_hosts=internal_hosts)
            ),
            correlation_id=_bounded_correlation_id(
                correlation_id,
                internal_hosts=internal_hosts,
            ),
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
        events = list(
            self.session.scalars(
                select(Event)
                .where(Event.project_id == project_id)
                .order_by(Event.created_at, Event.id)
                .limit(EVENT_SCAN_LIMIT + 1)
            )
        )
        if len(events) > EVENT_SCAN_LIMIT:
            raise ValueError(
                "event list exceeds 5000 records; use POST /v1/events/query"
            )
        return events

    def get_project_event(
        self,
        project_id: str,
        app_id: str,
        event_id: str,
    ) -> Event:
        app_id = validate_scope_id(app_id, field_name="app_id")
        event = self.session.scalar(
            select(Event).where(
                Event.project_id == project_id,
                Event.id == event_id,
                or_(Event.app_id == app_id, Event.app_id.is_(None)),
            )
        )
        if event is None or not _matches_event_scope(
            event.request_json,
            app_id,
            {},
            event.app_id,
            event.user_id,
            event.agent_id,
            event.run_id,
        ):
            raise KeyError(event_id)
        return event

    def mark_succeeded(self, event_id: str, *, response: dict[str, Any]) -> Event:
        event = self.get(event_id)
        internal_hosts = _project_internal_hosts(self.session, event.project_id)
        summary_response = {
            "results": _response_value(response, "results"),
            "total": _response_value(response, "total"),
        }
        result_count, previews = trace_result_summary(summary_response)
        response_document = _bounded_response_envelope(response)
        if previews:
            response_document["result_previews"] = previews
        omitted, scan_truncated = _preview_omission_details(
            summary_response,
            previews,
        )
        if omitted:
            response_document["result_previews_omitted"] = omitted
        if scan_truncated:
            response_document["result_previews_scan_truncated"] = True
        event.status = EventStatus.SUCCEEDED
        event.response_json = _trace_json(
            _safe_trace_document(
                response_document,
                internal_hosts=internal_hosts,
            )
        )
        event.result_count = result_count
        event.has_results = 1 if result_count else 0
        self._complete(event)
        self.session.flush()
        return event

    def mark_failed(self, event_id: str, *, error: dict[str, Any]) -> Event:
        event = self.get(event_id)
        internal_hosts = _project_internal_hosts(self.session, event.project_id)
        event.status = EventStatus.FAILED
        event.error_json = _trace_json(
            _safe_trace_document(error, internal_hosts=internal_hosts)
        )
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
        app_id = validate_scope_id(app_id, field_name="app_id")
        conditions = [
            Event.project_id == project_id,
            or_(Event.app_id == app_id, Event.app_id.is_(None)),
        ]
        entity_columns = {
            "user_id": Event.user_id,
            "agent_id": Event.agent_id,
            "run_id": Event.run_id,
        }
        validated_entity_filters: dict[str, str] = {}
        for field_name, expected in query.entity_filters.items():
            if field_name == "app_id":
                validated_entity_filters[field_name] = validate_scope_id(
                    expected, field_name=field_name
                )
                continue
            column = entity_columns.get(field_name)
            if column is None:
                validated_entity_filters[field_name] = expected
                continue
            validated = validate_scope_id(expected, field_name=field_name)
            validated_entity_filters[field_name] = validated
            conditions.append(or_(column == validated, column.is_(None)))
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

        for _attempt in range(_EVENT_QUERY_MAX_ATTEMPTS):
            page = self._query_project_events_snapshot(
                project_id=project_id,
                app_id=app_id,
                query=query,
                entity_filters=validated_entity_filters,
                conditions=conditions,
            )
            if page is not None:
                return page
        raise ValueError(_EVENT_QUERY_UNSTABLE_ERROR)

    def _query_project_events_snapshot(
        self,
        *,
        project_id: str,
        app_id: str,
        query: EventQuery,
        entity_filters: Mapping[str, str],
        conditions: list[object],
    ) -> EventPage | None:
        rows = list(
            self.session.execute(
                select(
                    Event.id,
                    Event.app_id,
                    Event.user_id,
                    Event.agent_id,
                    Event.run_id,
                    Event.request_json,
                    Event.created_at,
                    Event.operation,
                    Event.status,
                    Event.has_results,
                    Event.result_count,
                    Event.correlation_id,
                    Event.latency_ms,
                    Event.started_at,
                    Event.completed_at,
                    Event.subject_type,
                    Event.subject_id,
                )
                .where(*conditions)
                .order_by(Event.created_at.desc(), Event.id.desc())
                .limit(EVENT_SCAN_LIMIT + 1)
            )
        )
        if len(rows) > EVENT_SCAN_LIMIT:
            raise ValueError("entity filter scan exceeds 5000 records")
        candidates = [_EventCandidate(*row) for row in rows]
        matches = [
            candidate
            for candidate in candidates
            if _matches_event_scope(
                candidate.request_json,
                app_id,
                entity_filters,
                candidate.app_id,
                candidate.user_id,
                candidate.agent_id,
                candidate.run_id,
            )
        ]
        offset = (query.page - 1) * query.page_size
        page_candidates = matches[offset : offset + query.page_size]
        if page_candidates:
            loaded_values = list(
                self.session.scalars(
                    select(Event).where(
                        Event.project_id == project_id,
                        Event.id.in_(
                            [candidate.event_id for candidate in page_candidates]
                        ),
                    )
                )
            )
            loaded_events = {
                event.id: event for event in loaded_values if isinstance(event, Event)
            }
            if len(loaded_values) != len(page_candidates) or len(loaded_events) != len(
                page_candidates
            ):
                return None
            if any(
                not _event_matches_candidate(
                    loaded_events.get(candidate.event_id),
                    candidate,
                )
                for candidate in page_candidates
            ):
                return None
            items = [loaded_events[candidate.event_id] for candidate in page_candidates]
        else:
            items = []
        return EventPage(
            items=items,
            total=len(matches),
            buckets=_event_timeline_buckets(
                [candidate.created_at for candidate in matches],
                query,
            ),
        )


class MutationIntentFenceError(RuntimeError):
    """A stale durable-mutation worker no longer owns its attempt token."""


class MutationIntentRepository:
    """Persist bounded repair state before an upstream mutation can begin."""

    MAX_TARGETS = 5000
    RECOVERY_LIMIT = 100
    LEASE_SECONDS = 300
    MAX_ATTEMPTS = 3
    RECOVERABLE_STATUSES = ("ACTIVE", "UNKNOWN", "PENDING")
    BLOCKING_STATUSES = ("ACTIVE", "UNKNOWN", "PENDING", "EXHAUSTED")
    TERMINAL_STATUSES = ("COMPLETED", "FAILED", "PARTIAL")

    def __init__(self, session: Session) -> None:
        self.session = session

    def sanitize_payload(
        self,
        project_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return _safe_trace_document(
            payload,
            internal_hosts=_project_internal_hosts(self.session, project_id),
        )

    def create(
        self,
        *,
        project_id: str,
        app_id: str,
        event_id: str,
        operation: str,
        payload: dict[str, Any],
        memory_ids: Iterable[str] = (),
        operation_key: str | None = None,
    ) -> MutationIntent:
        target_ids = list(dict.fromkeys(memory_ids))
        if len(target_ids) > self.MAX_TARGETS:
            raise ValueError("mutation intent exceeds 5000 memory targets")
        now = _utc_now()
        intent = MutationIntent(
            project_id=project_id,
            app_id=app_id,
            event_id=event_id,
            operation=operation,
            operation_key=operation_key or secrets.token_hex(32),
            status="ACTIVE",
            payload_json=_trace_json(self.sanitize_payload(project_id, payload)),
            attempt_count=1,
            lease_expires_at=now + timedelta(seconds=self.LEASE_SECONDS),
            created_at=now,
            updated_at=now,
        )
        self.session.add(intent)
        self.session.flush()
        self.add_targets(intent.id, target_ids)
        return intent

    def find_by_operation_key(
        self,
        *,
        project_id: str,
        app_id: str,
        operation: str,
        operation_key: str,
    ) -> MutationIntent | None:
        return self.session.scalar(
            select(MutationIntent).where(
                MutationIntent.project_id == project_id,
                MutationIntent.app_id == app_id,
                MutationIntent.operation == operation,
                MutationIntent.operation_key == operation_key,
            )
        )

    def result(self, intent: MutationIntent) -> dict[str, Any]:
        try:
            result = json.loads(intent.result_json)
        except (TypeError, ValueError):
            return {}
        return result if isinstance(result, dict) else {}

    def add_targets(
        self,
        intent_id: str,
        memory_ids: Iterable[str],
    ) -> list[MutationIntentTarget]:
        current = self.targets(intent_id)
        existing = {target.memory_id for target in current}
        new_ids = [item for item in dict.fromkeys(memory_ids) if item not in existing]
        if len(current) + len(new_ids) > self.MAX_TARGETS:
            raise ValueError("mutation intent exceeds 5000 memory targets")
        targets = [
            MutationIntentTarget(
                intent_id=intent_id,
                memory_id=memory_id,
                ordinal=len(current) + ordinal,
            )
            for ordinal, memory_id in enumerate(new_ids)
        ]
        self.session.add_all(targets)
        self.session.flush()
        return targets

    def get(self, intent_id: str) -> MutationIntent:
        intent = self.session.get(MutationIntent, intent_id)
        if intent is None:
            raise KeyError(intent_id)
        return intent

    def payload(self, intent: MutationIntent) -> dict[str, Any]:
        try:
            payload = json.loads(intent.payload_json)
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def list_recoverable(self, project_id: str, app_id: str) -> list[MutationIntent]:
        now = _utc_now()
        return list(
            self.session.scalars(
                select(MutationIntent)
                .where(
                    MutationIntent.project_id == project_id,
                    MutationIntent.app_id == app_id,
                    or_(
                        MutationIntent.status.in_(("UNKNOWN", "PENDING")),
                        and_(
                            MutationIntent.status == "ACTIVE",
                            or_(
                                MutationIntent.lease_expires_at.is_(None),
                                MutationIntent.lease_expires_at <= now,
                            ),
                        ),
                    ),
                )
                .order_by(MutationIntent.created_at, MutationIntent.id)
                .limit(self.RECOVERY_LIMIT)
            )
        )

    def list_blocking(self, project_id: str, app_id: str) -> list[MutationIntent]:
        return list(
            self.session.scalars(
                select(MutationIntent)
                .where(
                    MutationIntent.project_id == project_id,
                    MutationIntent.app_id == app_id,
                    MutationIntent.status.in_(self.BLOCKING_STATUSES),
                )
                .order_by(MutationIntent.created_at, MutationIntent.id)
                .limit(self.RECOVERY_LIMIT)
            )
        )

    def targets(
        self,
        intent_id: str,
        *,
        pending_only: bool = False,
    ) -> list[MutationIntentTarget]:
        statement = select(MutationIntentTarget).where(
            MutationIntentTarget.intent_id == intent_id
        )
        if pending_only:
            statement = statement.where(MutationIntentTarget.status == "PENDING")
        return list(
            self.session.scalars(
                statement.order_by(
                    MutationIntentTarget.ordinal,
                    MutationIntentTarget.memory_id,
                )
            )
        )

    def claim_recovery(self, intent: MutationIntent) -> bool:
        now = _utc_now()
        if intent.attempt_count >= self.MAX_ATTEMPTS:
            self.mark_unresolved(
                intent.id,
                error={"message": "Mutation recovery attempts exhausted"},
            )
            return False
        intent.attempt_count += 1
        intent.status = "ACTIVE"
        intent.lease_expires_at = now + timedelta(seconds=self.LEASE_SECONDS)
        intent.updated_at = now
        self.session.flush()
        return True

    def require_active_attempt(
        self,
        intent_id: str,
        expected_attempt_count: int,
    ) -> MutationIntent:
        if type(expected_attempt_count) is not int or expected_attempt_count < 1:
            raise ValueError("expected mutation attempt count is invalid")
        intent = self.session.scalar(
            select(MutationIntent)
            .where(
                MutationIntent.id == intent_id,
                MutationIntent.status == "ACTIVE",
                MutationIntent.attempt_count == expected_attempt_count,
            )
            .execution_options(populate_existing=True)
        )
        if intent is None:
            raise MutationIntentFenceError(
                "durable mutation attempt lost its fence"
            )
        return intent

    def renew_active_attempt(
        self,
        intent_id: str,
        expected_attempt_count: int,
    ) -> MutationIntent:
        intent = self.require_active_attempt(intent_id, expected_attempt_count)
        now = _utc_now()
        intent.lease_expires_at = now + timedelta(seconds=self.LEASE_SECONDS)
        intent.updated_at = now
        self.session.flush()
        return intent

    def mark_target_succeeded(self, target: MutationIntentTarget) -> None:
        target.status = "COMPLETED"
        target.error_json = "{}"
        target.updated_at = _utc_now()
        self.session.flush()

    def mark_target_failed(
        self,
        target: MutationIntentTarget,
        error: dict[str, Any],
    ) -> None:
        intent = self.get(target.intent_id)
        target.status = "FAILED"
        target.error_json = _trace_json(
            _safe_trace_document(
                error,
                internal_hosts=_project_internal_hosts(
                    self.session, intent.project_id
                ),
            )
        )
        target.updated_at = _utc_now()
        self.session.flush()

    def mark_unresolved(
        self,
        intent_id: str,
        *,
        error: dict[str, Any] | None = None,
    ) -> MutationIntent:
        intent = self.get(intent_id)
        intent.status = (
            "EXHAUSTED"
            if intent.attempt_count >= self.MAX_ATTEMPTS
            else "UNKNOWN"
        )
        intent.lease_expires_at = None
        intent.updated_at = _utc_now()
        if error is not None:
            intent.error_json = _trace_json(
                _safe_trace_document(
                    error,
                    internal_hosts=_project_internal_hosts(
                        self.session, intent.project_id
                    ),
                )
            )
        self.session.flush()
        return intent

    def fail(
        self,
        intent_id: str,
        *,
        error: dict[str, Any],
        status: str = "FAILED",
        result: dict[str, Any] | None = None,
    ) -> MutationIntent:
        if status not in {"FAILED", "PARTIAL"}:
            raise ValueError("terminal mutation failure status is invalid")
        intent = self.get(intent_id)
        now = _utc_now()
        intent.status = status
        intent.error_json = _trace_json(
            _safe_trace_document(
                error,
                internal_hosts=_project_internal_hosts(
                    self.session, intent.project_id
                ),
            )
        )
        if result is not None:
            intent.result_json = _trace_json(
                _safe_trace_document(
                    result,
                    internal_hosts=_project_internal_hosts(
                        self.session, intent.project_id
                    ),
                )
            )
        intent.lease_expires_at = None
        intent.completed_at = now
        intent.updated_at = now
        self.session.flush()
        return intent

    def release_for_recovery(
        self,
        intent_id: str,
        *,
        error: dict[str, Any] | None = None,
    ) -> MutationIntent:
        """Normalize legacy callers into the explicit ambiguous state."""

        return self.mark_unresolved(intent_id, error=error)

    def complete(
        self,
        intent_id: str,
        *,
        result: dict[str, Any],
    ) -> MutationIntent:
        intent = self.get(intent_id)
        now = _utc_now()
        intent.status = "COMPLETED"
        intent.result_json = _trace_json(
            _safe_trace_document(
                result,
                internal_hosts=_project_internal_hosts(
                    self.session, intent.project_id
                ),
            )
        )
        intent.error_json = "{}"
        intent.lease_expires_at = None
        intent.completed_at = now
        intent.updated_at = now
        self.session.flush()
        return intent


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

    def list_memories_by_ids(
        self,
        *,
        project_id: str,
        app_id: str | None,
        mem0_memory_ids: Iterable[str],
        include_deleted: bool = False,
        include_shadowed: bool = False,
    ) -> list[MemoryIndex]:
        memory_ids = list(dict.fromkeys(mem0_memory_ids))
        memories: list[MemoryIndex] = []
        for offset in range(0, len(memory_ids), 400):
            statement = select(MemoryIndex).where(
                MemoryIndex.project_id == project_id,
                MemoryIndex.mem0_memory_id.in_(memory_ids[offset : offset + 400]),
            )
            if app_id is not None:
                statement = statement.where(MemoryIndex.app_id == app_id)
            if not include_deleted:
                statement = statement.where(MemoryIndex.deleted_at.is_(None))
            if not include_shadowed:
                statement = statement.where(
                    MemoryIndex.consolidation_state == "ACTIVE"
                )
            memories.extend(self.session.scalars(statement))
        return memories

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
            MemoryIndex.consolidation_state == "ACTIVE",
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
                MemoryIndex.consolidation_state == "ACTIVE",
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
        if (memory_ids := filters.get("memory_ids")) is not None:
            if (
                not isinstance(memory_ids, list)
                or not memory_ids
                or len(memory_ids) > 1000
                or any(not isinstance(item, str) or not item for item in memory_ids)
            ):
                raise ValueError("memory_ids export filter is invalid")
            statement = statement.where(
                MemoryIndex.mem0_memory_id.in_(list(dict.fromkeys(memory_ids)))
            )
        return list(self.session.scalars(statement))

    def query_project_memories(
        self,
        project_id: str,
        app_id: str | None,
        query: ExplorerQuery,
        *,
        window_offset: int | None = None,
        window_limit: int | None = None,
    ) -> MemoryIndexPage:
        offset = (
            (query.page - 1) * query.page_size
            if window_offset is None
            else window_offset
        )
        limit = query.page_size if window_limit is None else window_limit
        if offset < 0 or limit < 1:
            raise ValueError("memory query window is invalid")
        scope_conditions = [
            MemoryIndex.project_id == project_id,
            MemoryIndex.deleted_at.is_(None),
            MemoryIndex.consolidation_state == "ACTIVE",
        ]
        if app_id is not None:
            scope_conditions.append(MemoryIndex.app_id == app_id)
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
            statement = (
                select(MemoryIndex)
                .where(*conditions)
                .order_by(*_memory_order_by(query))
                .offset(offset)
                .limit(limit)
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
        if scan_count > EXPLORER_RECORD_HORIZON:
            raise ValueError("metadata filter scan exceeds 5000 records")

        candidates = list(
            self.session.scalars(
                select(MemoryIndex)
                .where(*candidate_conditions)
                .order_by(*_memory_order_by(query))
                .limit(EXPLORER_RECORD_HORIZON + 1)
            )
        )
        if len(candidates) > EXPLORER_RECORD_HORIZON:
            raise ValueError("metadata filter scan exceeds 5000 records")
        scan_count = len(candidates)
        matches = [
            memory
            for memory in candidates
            if _matches_query_filters(memory, query)
        ]
        return MemoryIndexPage(
            items=matches[offset : offset + limit],
            total=len(matches),
            scan_count=scan_count,
        )

    def list_reconcile_stale_candidates(
        self,
        *,
        project_id: str,
        app_id: str | None,
        updated_at_lte: datetime,
        after_created_at: datetime | None = None,
        after_memory_id: str | None = None,
        limit: int = 200,
    ) -> list[MemoryIndex]:
        if limit < 1 or limit > 200:
            raise ValueError(
                "reconcile candidate batch limit must be between 1 and 200"
            )
        if (after_created_at is None) != (after_memory_id is None):
            raise ValueError("reconcile candidate cursor is incomplete")

        statement = select(MemoryIndex).where(
            MemoryIndex.project_id == project_id,
            MemoryIndex.deleted_at.is_(None),
            MemoryIndex.updated_at <= updated_at_lte,
        )
        if app_id is not None:
            statement = statement.where(MemoryIndex.app_id == app_id)
        if after_created_at is not None and after_memory_id is not None:
            statement = statement.where(
                or_(
                    MemoryIndex.created_at > after_created_at,
                    and_(
                        MemoryIndex.created_at == after_created_at,
                        MemoryIndex.mem0_memory_id > after_memory_id,
                    ),
                )
            )
        return list(
            self.session.scalars(
                statement.order_by(
                    MemoryIndex.created_at,
                    MemoryIndex.mem0_memory_id,
                ).limit(limit)
            )
        )

    def mark_scope_dirty(
        self,
        project_id: str,
        app_id: str,
        observed_at: datetime,
    ) -> None:
        self.session.execute(
            update(MemoryIndex)
            .where(
                MemoryIndex.project_id == project_id,
                MemoryIndex.app_id == app_id,
                MemoryIndex.deleted_at.is_(None),
                MemoryIndex.consolidation_state == "ACTIVE",
                MemoryIndex.last_observed_at.is_(None),
            )
            .values(last_observed_at=observed_at)
            .execution_options(synchronize_session="fetch")
        )
        self.session.flush()

    def list_scope_marker_backfill_candidates(
        self,
        *,
        project_id: str,
        app_id: str,
        retry_before: datetime,
        limit: int = 200,
    ) -> list[MemoryIndex]:
        if limit < 1 or limit > 1000:
            raise ValueError("scope marker backfill limit must be between 1 and 1000")
        return list(
            self.session.scalars(
                select(MemoryIndex)
                .where(
                    MemoryIndex.project_id == project_id,
                    MemoryIndex.app_id == app_id,
                    MemoryIndex.deleted_at.is_(None),
                    MemoryIndex.scope_markers_verified == 0,
                    or_(
                        MemoryIndex.scope_marker_backfill_attempted_at.is_(None),
                        MemoryIndex.scope_marker_backfill_attempted_at
                        <= retry_before,
                    ),
                )
                .order_by(
                    case(
                        (
                            MemoryIndex.scope_marker_backfill_attempted_at.is_(None),
                            0,
                        ),
                        else_=1,
                    ),
                    MemoryIndex.scope_marker_backfill_attempted_at,
                    MemoryIndex.created_at,
                    MemoryIndex.mem0_memory_id,
                )
                .limit(limit)
            )
        )

    def count_scope_marker_backfill_required(
        self,
        *,
        project_id: str,
        app_id: str,
    ) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(MemoryIndex)
                .where(
                    MemoryIndex.project_id == project_id,
                    MemoryIndex.app_id == app_id,
                    MemoryIndex.deleted_at.is_(None),
                    MemoryIndex.scope_markers_verified == 0,
                )
            )
            or 0
        )

    def mark_scope_markers_verified(
        self,
        *,
        project_id: str,
        app_id: str,
        memory_id: str,
        verified_at: datetime | None = None,
    ) -> bool:
        result = self.session.execute(
            update(MemoryIndex)
            .where(
                MemoryIndex.project_id == project_id,
                MemoryIndex.app_id == app_id,
                MemoryIndex.mem0_memory_id == memory_id,
                MemoryIndex.deleted_at.is_(None),
            )
            .values(
                scope_markers_verified=1,
                scope_marker_backfill_status="VERIFIED",
                scope_marker_backfill_attempted_at=verified_at or _utc_now(),
            )
            .execution_options(synchronize_session="fetch")
        )
        self.session.flush()
        return bool(result.rowcount)

    def mark_scope_marker_backfill_outcome(
        self,
        *,
        project_id: str,
        app_id: str,
        memory_id: str,
        status: str,
        attempted_at: datetime,
    ) -> bool:
        if status not in {"CONFLICT", "MISSING"}:
            raise ValueError("invalid scope marker backfill outcome")
        result = self.session.execute(
            update(MemoryIndex)
            .where(
                MemoryIndex.project_id == project_id,
                MemoryIndex.app_id == app_id,
                MemoryIndex.mem0_memory_id == memory_id,
                MemoryIndex.deleted_at.is_(None),
                MemoryIndex.scope_markers_verified == 0,
            )
            .values(
                scope_marker_backfill_status=status,
                scope_marker_backfill_attempted_at=attempted_at,
            )
            .execution_options(synchronize_session="fetch")
        )
        self.session.flush()
        return bool(result.rowcount)

    def scope_marker_backfill_counts(
        self,
        *,
        project_id: str,
        app_id: str,
    ) -> dict[str, int]:
        rows = self.session.execute(
            select(MemoryIndex.scope_marker_backfill_status, func.count())
            .where(
                MemoryIndex.project_id == project_id,
                MemoryIndex.app_id == app_id,
                MemoryIndex.deleted_at.is_(None),
                MemoryIndex.scope_markers_verified == 0,
            )
            .group_by(MemoryIndex.scope_marker_backfill_status)
        )
        return {status: int(count) for status, count in rows}

    def list_dirty_anchors(
        self,
        *,
        project_id: str,
        app_id: str,
        after_last_observed_at: datetime | None = None,
        after_memory_id: str | None = None,
        limit: int = 100,
    ) -> list[MemoryIndex]:
        if limit < 1 or limit > 1000:
            raise ValueError("dirty anchor limit must be between 1 and 1000")
        if (after_last_observed_at is None) != (after_memory_id is None):
            raise ValueError("dirty anchor cursor is incomplete")

        statement = select(MemoryIndex).where(
            MemoryIndex.project_id == project_id,
            MemoryIndex.app_id == app_id,
            MemoryIndex.deleted_at.is_(None),
            MemoryIndex.consolidation_state == "ACTIVE",
            MemoryIndex.pinned == 0,
            MemoryIndex.scope_markers_verified == 1,
            MemoryIndex.content_hash.is_not(None),
            MemoryIndex.last_observed_at.is_not(None),
            or_(
                MemoryIndex.last_consolidation_scan_at.is_(None),
                MemoryIndex.last_observed_at
                > MemoryIndex.last_consolidation_scan_at,
            ),
        )
        if after_last_observed_at is not None and after_memory_id is not None:
            statement = statement.where(
                or_(
                    MemoryIndex.last_observed_at > after_last_observed_at,
                    and_(
                        MemoryIndex.last_observed_at == after_last_observed_at,
                        MemoryIndex.mem0_memory_id > after_memory_id,
                    ),
                )
            )
        return list(
            self.session.scalars(
                statement.order_by(
                    MemoryIndex.last_observed_at,
                    MemoryIndex.mem0_memory_id,
                ).limit(limit)
            )
        )

    def mark_anchors_scanned(
        self,
        *,
        project_id: str,
        app_id: str,
        memory_ids: list[str],
        scan_cutoff: datetime,
    ) -> int:
        if not memory_ids:
            return 0
        result = self.session.execute(
            update(MemoryIndex)
            .where(
                MemoryIndex.project_id == project_id,
                MemoryIndex.app_id == app_id,
                MemoryIndex.mem0_memory_id.in_(memory_ids),
                MemoryIndex.last_observed_at.is_not(None),
                MemoryIndex.last_observed_at <= scan_cutoff,
            )
            .values(last_consolidation_scan_at=scan_cutoff)
            .execution_options(synchronize_session="fetch")
        )
        self.session.flush()
        return int(result.rowcount or 0)

    def count_dirty_anchors(self, *, project_id: str, app_id: str) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(MemoryIndex)
                .where(
                    MemoryIndex.project_id == project_id,
                    MemoryIndex.app_id == app_id,
                    MemoryIndex.deleted_at.is_(None),
                    MemoryIndex.consolidation_state == "ACTIVE",
                    MemoryIndex.pinned == 0,
                    MemoryIndex.scope_markers_verified == 1,
                    MemoryIndex.content_hash.is_not(None),
                    MemoryIndex.last_observed_at.is_not(None),
                    or_(
                        MemoryIndex.last_consolidation_scan_at.is_(None),
                        MemoryIndex.last_observed_at
                        > MemoryIndex.last_consolidation_scan_at,
                    ),
                )
            )
            or 0
        )

    def list_exact_group_peers(
        self,
        anchor: MemoryIndex,
        *,
        limit: int = 100,
    ) -> list[MemoryIndex]:
        if limit < 1 or limit > 100:
            raise ValueError("exact peer limit must be between 1 and 100")
        if not anchor.app_id or not anchor.content_hash:
            return []

        statement = select(MemoryIndex).where(
            MemoryIndex.project_id == anchor.project_id,
            MemoryIndex.app_id == anchor.app_id,
            MemoryIndex.deleted_at.is_(None),
            MemoryIndex.consolidation_state == "ACTIVE",
            MemoryIndex.pinned == 0,
            MemoryIndex.scope_markers_verified == 1,
            MemoryIndex.normalized_type == anchor.normalized_type,
            MemoryIndex.content_hash == anchor.content_hash,
        )
        for column, value in (
            (MemoryIndex.user_id, anchor.user_id),
            (MemoryIndex.agent_id, anchor.agent_id),
        ):
            statement = statement.where(
                column.is_(None) if value is None else column == value
            )
        return list(
            self.session.scalars(
                statement.order_by(MemoryIndex.mem0_memory_id).limit(limit)
            )
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
        app_id: str | None,
        mem0_memory_ids: Iterable[str],
        updated_at_lte: datetime,
        expected_updated_at: Mapping[str, datetime] | None = None,
    ) -> int:
        memory_ids = list(dict.fromkeys(mem0_memory_ids))
        if not memory_ids:
            return 0

        batches = [
            memory_ids[offset : offset + 200]
            for offset in range(0, len(memory_ids), 200)
        ]

        marked = 0
        stale_at = _utc_now()
        for batch in batches:
            statement = update(MemoryIndex).where(
                MemoryIndex.project_id == project_id,
                MemoryIndex.mem0_memory_id.in_(batch),
                MemoryIndex.deleted_at.is_(None),
                MemoryIndex.updated_at <= updated_at_lte,
            )
            if app_id is not None:
                statement = statement.where(MemoryIndex.app_id == app_id)
            if expected_updated_at is not None:
                statement = statement.where(
                    or_(
                        *(
                            and_(
                                MemoryIndex.mem0_memory_id == memory_id,
                                MemoryIndex.updated_at
                                == expected_updated_at[memory_id],
                            )
                            for memory_id in batch
                        )
                    )
                )
            result = self.session.execute(
                statement.values(deleted_at=stale_at).execution_options(
                    synchronize_session="fetch"
                )
            )
            marked += result.rowcount or 0
        self.session.flush()
        return marked

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
        content_hash: str | None = None,
        content_length: int | None = None,
        normalized_type: str | None = None,
        source: str | None = None,
        pinned: bool | int | None = None,
        expires_at: datetime | None = None,
        scope_markers_verified: bool | int | None = True,
        observed_at: datetime | None = None,
        preserve_observed_at_on_noop: bool = False,
    ) -> MemoryIndex:
        projection_now = _utc_now()
        memory = self.session.scalar(
            select(MemoryIndex).where(
                MemoryIndex.project_id == project_id,
                MemoryIndex.mem0_memory_id == mem0_memory_id,
            )
        )
        is_new = memory is None
        if memory is None:
            memory = MemoryIndex(project_id=project_id, mem0_memory_id=mem0_memory_id)
            self.session.add(memory)

        metadata_projection_json = _json(metadata or {})
        verified = int(bool(scope_markers_verified))
        expires_at_changed = (
            (memory.expires_at is None) != (expires_at is None)
            or (
                memory.expires_at is not None
                and expires_at is not None
                and _as_utc(memory.expires_at) != _as_utc(expires_at)
            )
        )
        observed_projection_changed = (
            is_new
            or memory.deleted_at is not None
            or memory.user_id != user_id
            or memory.agent_id != agent_id
            or memory.app_id != app_id
            or memory.run_id != run_id
            or memory.category != category
            or memory.metadata_projection_json != metadata_projection_json
            or memory.content_hash != content_hash
            or memory.content_length != content_length
            or memory.normalized_type != normalized_type
            or memory.source != source
            or memory.pinned != int(bool(pinned))
            or expires_at_changed
            or memory.scope_markers_verified != verified
        )
        previous_hash = memory.content_hash
        previously_verified = memory.scope_markers_verified == 1

        memory.user_id = user_id
        memory.agent_id = agent_id
        memory.app_id = app_id
        memory.run_id = run_id
        memory.category = category
        memory.metadata_projection_json = metadata_projection_json
        if observed_at is not None:
            if memory.deleted_at is not None or previous_hash != content_hash:
                memory.consolidation_state = "ACTIVE"
                memory.shadowed_by_proposal_id = None
            memory.content_hash = content_hash
            memory.content_length = content_length
            memory.normalized_type = normalized_type
            memory.source = source
            memory.pinned = int(bool(pinned))
            memory.expires_at = expires_at
            memory.scope_markers_verified = verified
            if memory.scope_markers_verified == 1:
                memory.scope_marker_backfill_status = "VERIFIED"
                memory.scope_marker_backfill_attempted_at = observed_at
            elif is_new or previously_verified:
                memory.scope_marker_backfill_status = "PENDING"
                memory.scope_marker_backfill_attempted_at = None
            if not preserve_observed_at_on_noop or observed_projection_changed:
                memory.last_observed_at = observed_at
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
        content_hash: str | None = None,
        content_length: int | None = None,
        normalized_type: str | None = None,
        source: str | None = None,
        pinned: bool | int | None = None,
        expires_at: datetime | None = None,
        scope_markers_verified: bool | int | None = True,
        observed_at: datetime | None = None,
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
        if observed_at is not None:
            verified = int(bool(scope_markers_verified))
            values.update(
                {
                    "content_hash": content_hash,
                    "content_length": content_length,
                    "normalized_type": normalized_type,
                    "source": source,
                    "pinned": int(bool(pinned)),
                    "expires_at": expires_at,
                    "scope_markers_verified": verified,
                    "scope_marker_backfill_status": (
                        "VERIFIED" if verified else "PENDING"
                    ),
                    "scope_marker_backfill_attempted_at": (
                        observed_at if verified else None
                    ),
                    "last_observed_at": observed_at,
                    "consolidation_state": "ACTIVE",
                    "shadowed_by_proposal_id": None,
                }
            )

        def update_claimable() -> int:
            update_values = dict(values)
            if observed_at is not None:
                same_hash = and_(
                    MemoryIndex.deleted_at.is_(None),
                    MemoryIndex.content_hash == content_hash,
                )
                update_values["consolidation_state"] = case(
                    (same_hash, MemoryIndex.consolidation_state),
                    else_="ACTIVE",
                )
                update_values["shadowed_by_proposal_id"] = case(
                    (same_hash, MemoryIndex.shadowed_by_proposal_id),
                    else_=None,
                )
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
                .values(**update_values)
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
    _MEMORY_ID_COLUMNS = {
        "user": MemoryIndex.user_id,
        "agent": MemoryIndex.agent_id,
        "app": MemoryIndex.app_id,
        "run": MemoryIndex.run_id,
    }

    def __init__(self, session: Session) -> None:
        self.session = session

    @classmethod
    def _memory_id_column(cls, entity_type: str):
        if type(entity_type) is not str or entity_type not in cls._MEMORY_ID_COLUMNS:
            raise ValueError("Unsupported entity type")
        return cls._MEMORY_ID_COLUMNS[entity_type]

    def upsert_entity(
        self,
        *,
        project_id: str,
        app_id: str | None = None,
        entity_type: str,
        entity_id: str,
        display_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Entity:
        self._memory_id_column(entity_type)
        if app_id is None:
            if entity_type != "app":
                raise ValueError("app_id is required")
            app_id = entity_id
        entity = self.session.scalar(
            select(Entity).where(
                Entity.project_id == project_id,
                Entity.app_id == app_id,
                Entity.entity_type == entity_type,
                Entity.entity_id == entity_id,
            )
        )
        if entity is None:
            entity = Entity(
                project_id=project_id,
                app_id=app_id,
                entity_type=entity_type,
                entity_id=entity_id,
            )
            self.session.add(entity)

        entity.display_name = display_name
        entity.metadata_json = _json(metadata or {})
        entity.last_seen_at = _utc_now()
        self.session.flush()
        return entity

    def rebuild_project_entities(
        self,
        project_id: str,
        app_id: str,
    ) -> list[Entity]:
        ProjectRepository(self.session).lock_for_mutation(project_id)
        with self.session.no_autoflush:
            memories = list(
                self.session.scalars(
                    select(MemoryIndex).where(
                        MemoryIndex.project_id == project_id,
                        MemoryIndex.app_id == app_id,
                        MemoryIndex.deleted_at.is_(None),
                        MemoryIndex.consolidation_state == "ACTIVE",
                    )
                    .order_by(MemoryIndex.id)
                    .limit(EXPLORER_RECORD_HORIZON + 1)
                )
            )
            if len(memories) > EXPLORER_RECORD_HORIZON:
                raise ValueError(
                    "entity rebuild exceeds "
                    f"{EXPLORER_RECORD_HORIZON} active memories"
                )
            aggregates: dict[tuple[str, str], tuple[int, datetime]] = {}
            identity_fields = (
                ("user", "user_id"),
                ("agent", "agent_id"),
                ("app", "app_id"),
                ("run", "run_id"),
            )
            for memory in memories:
                for entity_type, field_name in identity_fields:
                    entity_id = getattr(memory, field_name)
                    if entity_id is None:
                        continue
                    key = (entity_type, entity_id)
                    updated_at = _as_utc(memory.updated_at)
                    current = aggregates.get(key)
                    if current is None:
                        aggregates[key] = (1, updated_at)
                    else:
                        aggregates[key] = (
                            current[0] + 1,
                            max(current[1], updated_at),
                        )

            self.session.execute(
                delete(Entity)
                .where(
                    Entity.project_id == project_id,
                    Entity.app_id == app_id,
                )
                .execution_options(synchronize_session=False)
            )
            entities = [
                Entity(
                    project_id=project_id,
                    app_id=app_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    display_name=entity_id,
                    memory_count=count,
                    last_seen_at=last_seen_at,
                )
                for (entity_type, entity_id), (count, last_seen_at) in sorted(
                    aggregates.items()
                )
            ]
            self.session.add_all(entities)
        self.session.flush()
        return entities

    def refresh_affected_entities(
        self,
        project_id: str,
        app_id: str,
        identities: Iterable[tuple[str, str]],
    ) -> list[Entity]:
        """Refresh only entity rows whose source-memory membership changed."""
        normalized = sorted(set(identities))
        if len(normalized) > ENTITY_REFRESH_IDENTITY_LIMIT:
            raise ValueError("Too many entity identities to refresh")

        ids_by_type: dict[str, list[str]] = {}
        for entity_type, entity_id in normalized:
            self._memory_id_column(entity_type)
            if type(entity_id) is not str or not entity_id:
                raise ValueError("Entity ID must be a non-empty string")
            ids_by_type.setdefault(entity_type, []).append(entity_id)

        if not ids_by_type:
            return []

        self.session.flush()
        aggregates: dict[tuple[str, str], tuple[int, datetime]] = {}
        existing_by_key: dict[tuple[str, str], Entity] = {}
        for entity_type, entity_ids in ids_by_type.items():
            memory_id_column = self._memory_id_column(entity_type)
            rows = self.session.execute(
                select(
                    memory_id_column,
                    func.count(MemoryIndex.id),
                    func.max(MemoryIndex.updated_at),
                )
                .where(
                    MemoryIndex.project_id == project_id,
                    MemoryIndex.app_id == app_id,
                    MemoryIndex.deleted_at.is_(None),
                    MemoryIndex.consolidation_state == "ACTIVE",
                    memory_id_column.in_(entity_ids),
                )
                .group_by(memory_id_column)
            )
            for entity_id, count, last_seen_at in rows:
                if entity_id is None or last_seen_at is None:
                    continue
                aggregates[(entity_type, entity_id)] = (
                    int(count),
                    _as_utc(last_seen_at),
                )

            existing = self.session.scalars(
                select(Entity).where(
                    Entity.project_id == project_id,
                    Entity.app_id == app_id,
                    Entity.entity_type == entity_type,
                    Entity.entity_id.in_(entity_ids),
                )
            )
            existing_by_key.update(
                {(entity.entity_type, entity.entity_id): entity for entity in existing}
            )

        refreshed: list[Entity] = []
        for entity_type, entity_id in normalized:
            key = (entity_type, entity_id)
            aggregate = aggregates.get(key)
            entity = existing_by_key.get(key)
            if aggregate is None:
                if entity is not None:
                    self.session.delete(entity)
                continue
            if entity is None:
                entity = Entity(
                    project_id=project_id,
                    app_id=app_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                )
                self.session.add(entity)
            entity.display_name = entity_id
            entity.metadata_json = "{}"
            entity.memory_count, entity.last_seen_at = aggregate
            refreshed.append(entity)

        self.session.flush()
        return refreshed

    @classmethod
    def memory_identities(cls, memory: Any) -> set[tuple[str, str]]:
        identities: set[tuple[str, str]] = set()
        for entity_type, field_name in (
            ("user", "user_id"),
            ("agent", "agent_id"),
            ("app", "app_id"),
            ("run", "run_id"),
        ):
            entity_id = getattr(memory, field_name, None)
            if isinstance(entity_id, str) and entity_id:
                identities.add((entity_type, entity_id))
        return identities

    def refresh_affected_memories(
        self,
        project_id: str,
        app_id: str,
        memories: Iterable[Any],
    ) -> list[Entity]:
        """Refresh identities from old/new memory projections in bounded batches."""
        pending: set[tuple[str, str]] = set()
        refreshed_by_key: dict[tuple[str, str], Entity] = {}

        def flush_pending() -> None:
            if not pending:
                return
            for entity in self.refresh_affected_entities(
                project_id,
                app_id,
                pending,
            ):
                refreshed_by_key[(entity.entity_type, entity.entity_id)] = entity
            pending.clear()

        for memory in memories:
            identities = self.memory_identities(memory)
            if len(pending | identities) > ENTITY_REFRESH_IDENTITY_LIMIT:
                flush_pending()
            pending.update(identities)
        flush_pending()
        return [refreshed_by_key[key] for key in sorted(refreshed_by_key)]

    def get_project_entity(
        self,
        project_id: str,
        app_id: str,
        entity_type: str,
        entity_id: str,
    ) -> Entity:
        self._memory_id_column(entity_type)
        entity = self.session.scalar(
            select(Entity).where(
                Entity.project_id == project_id,
                Entity.app_id == app_id,
                Entity.entity_type == entity_type,
                Entity.entity_id == entity_id,
            )
        )
        if entity is None:
            raise KeyError(entity_id)
        return entity

    def list_entity_memory_ids(
        self,
        project_id: str,
        app_id: str,
        entity_type: str,
        entity_id: str,
    ) -> list[str]:
        identity_column = self._memory_id_column(entity_type)
        statement = (
            select(MemoryIndex.mem0_memory_id)
            .where(
                MemoryIndex.project_id == project_id,
                MemoryIndex.app_id == app_id,
                MemoryIndex.deleted_at.is_(None),
                MemoryIndex.consolidation_state == "ACTIVE",
                identity_column == entity_id,
            )
            .order_by(
                MemoryIndex.updated_at.desc(),
                MemoryIndex.mem0_memory_id.asc(),
            )
            .limit(MutationIntentRepository.MAX_TARGETS + 1)
        )
        memory_ids = list(self.session.scalars(statement))
        if len(memory_ids) > MutationIntentRepository.MAX_TARGETS:
            raise ValueError("mutation intent exceeds 5000 memory targets")
        return memory_ids


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
        run_after: datetime | None = None,
        max_attempts: int = 3,
        dedupe_key: str | None = None,
    ) -> Job:
        if max_attempts < 1 or max_attempts > 100:
            raise ValueError("max_attempts must be between 1 and 100")
        job = Job(
            project_id=project_id,
            event_id=event_id,
            job_type=job_type,
            payload_json=_json(payload),
            run_after=run_after,
            max_attempts=max_attempts,
            dedupe_key=dedupe_key,
        )
        self.session.add(job)
        self.session.flush()
        return job

    def _claim_candidate_statement(
        self,
        *,
        job_types: tuple[str, ...],
        now: datetime,
        skip_locked: bool,
    ):
        eligible = or_(
            and_(
                Job.status == JobStatus.PENDING,
                or_(Job.run_after.is_(None), Job.run_after <= now),
            ),
            and_(
                Job.status == JobStatus.RUNNING,
                Job.lease_expires_at.is_not(None),
                Job.lease_expires_at <= now,
            ),
        )
        statement = (
            select(Job.id)
            .where(eligible, Job.attempt_count < Job.max_attempts)
            .order_by(
                func.coalesce(Job.run_after, Job.created_at),
                Job.created_at,
                Job.id,
            )
            .limit(1)
        )
        if job_types:
            statement = statement.where(Job.job_type.in_(job_types))
        if skip_locked:
            statement = statement.with_for_update(skip_locked=True)
        return statement

    def claim_next(
        self,
        job_types: tuple[str, ...] = (),
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> Job | None:
        if lease_seconds < 1 or lease_seconds > 86_400:
            raise ValueError("lease_seconds must be between 1 and 86400")
        now = now or _utc_now()
        self.session.execute(
            update(Job)
            .where(
                Job.status == JobStatus.RUNNING,
                Job.lease_expires_at.is_not(None),
                Job.lease_expires_at <= now,
                Job.attempt_count >= Job.max_attempts,
            )
            .values(
                status=JobStatus.FAILED,
                lease_expires_at=None,
                completed_at=now,
                error_json=_json({"error_type": "AttemptsExhausted"}),
                updated_at=now,
            )
            .execution_options(synchronize_session="fetch")
        )
        candidate = self._claim_candidate_statement(
            job_types=job_types,
            now=now,
            skip_locked=self.session.get_bind().dialect.name == "postgresql",
        )
        claimed_id = self.session.scalar(
            update(Job)
            .where(Job.id == candidate.scalar_subquery())
            .values(
                status=JobStatus.RUNNING,
                locked_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                attempt_count=Job.attempt_count + 1,
                completed_at=None,
                updated_at=now,
            )
            .returning(Job.id)
            .execution_options(synchronize_session="fetch")
        )
        if claimed_id is None:
            return None
        return self.session.scalar(
            select(Job)
            .where(Job.id == claimed_id)
            .execution_options(populate_existing=True)
        )

    def payload(self, job: Job) -> dict[str, Any]:
        try:
            payload = json.loads(job.payload_json)
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def mark_succeeded(
        self,
        job_id: str,
        result: dict[str, object],
    ) -> Job:
        job = self.session.get(Job, job_id)
        if job is None:
            raise KeyError(job_id)
        now = _utc_now()
        job.status = JobStatus.SUCCEEDED
        job.result_json = _json(result)
        job.error_json = "{}"
        job.lease_expires_at = None
        job.completed_at = now
        job.updated_at = now
        self.session.flush()
        return job

    def mark_failed(
        self,
        job_id: str,
        error: dict[str, object],
        retry_at: datetime | None,
    ) -> Job:
        job = self.session.get(Job, job_id)
        if job is None:
            raise KeyError(job_id)
        now = _utc_now()
        should_retry = retry_at is not None and job.attempt_count < job.max_attempts
        job.status = JobStatus.PENDING if should_retry else JobStatus.FAILED
        job.error_json = _json(error)
        job.run_after = retry_at if should_retry else job.run_after
        job.locked_at = None
        job.lease_expires_at = None
        job.completed_at = None if should_retry else now
        job.updated_at = now
        self.session.flush()
        return job

    def consolidation_status(
        self,
        *,
        project_id: str,
        app_id: str,
        now: datetime | None = None,
    ) -> dict[str, object]:
        now = now or _utc_now()
        jobs = self.session.scalars(
            select(Job)
            .where(
                Job.project_id == project_id,
                Job.job_type.in_(("consolidation.scan", "consolidation.finalize")),
            )
            .order_by(Job.created_at.desc())
            .limit(5000)
        )
        matching = [
            job
            for job in jobs
            if self.payload(job).get("app_id") == app_id
        ]
        succeeded = [
            job
            for job in matching
            if job.status == JobStatus.SUCCEEDED and job.completed_at is not None
        ]
        pending = [job for job in matching if job.status == JobStatus.PENDING]
        last_success = max(
            (job.completed_at for job in succeeded if job.completed_at is not None),
            default=None,
        )
        oldest_pending = min(
            (job.created_at for job in pending),
            default=None,
        )
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        if oldest_pending is not None:
            if oldest_pending.tzinfo is None:
                oldest_pending = oldest_pending.replace(tzinfo=UTC)
            else:
                oldest_pending = oldest_pending.astimezone(UTC)
        return {
            "last_success_at": last_success,
            "pending_count": len(pending),
            "oldest_pending_age_seconds": (
                max((now - oldest_pending).total_seconds(), 0.0)
                if oldest_pending is not None
                else None
            ),
        }


class ConsolidationPolicyRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(
        self,
        *,
        project_id: str,
        app_id: str,
        policy: Mapping[str, object],
    ) -> ConsolidationPolicy:
        spec = ConsolidationPolicySpec.from_mapping(policy)
        row = self.session.scalar(
            select(ConsolidationPolicy).where(
                ConsolidationPolicy.project_id == project_id,
                ConsolidationPolicy.app_id == app_id,
            )
        )
        if row is None:
            row = ConsolidationPolicy(project_id=project_id, app_id=app_id)
            self.session.add(row)
        row.enabled = int(spec.enabled)
        row.mode = spec.mode
        row.version = spec.policy_version
        row.policy_json = _json(spec.to_mapping())
        row.updated_at = _utc_now()
        self.session.flush()
        return row

    def get(self, project_id: str, app_id: str) -> ConsolidationPolicy | None:
        return self.session.scalar(
            select(ConsolidationPolicy).where(
                ConsolidationPolicy.project_id == project_id,
                ConsolidationPolicy.app_id == app_id,
            )
        )

    def list_enabled(self) -> list[ConsolidationPolicy]:
        return list(
            self.session.scalars(
                select(ConsolidationPolicy)
                .where(ConsolidationPolicy.enabled == 1)
                .order_by(
                    ConsolidationPolicy.project_id,
                    ConsolidationPolicy.app_id,
                )
            )
        )

    def spec(self, row: ConsolidationPolicy) -> ConsolidationPolicySpec:
        try:
            value = json.loads(row.policy_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("stored consolidation policy is invalid") from exc
        if not isinstance(value, dict):
            raise ValueError("stored consolidation policy is invalid")
        return ConsolidationPolicySpec.from_mapping(value)


class ConsolidationRunRepository:
    NONTERMINAL = ("PENDING", "RUNNING")

    def __init__(self, session: Session) -> None:
        self.session = session

    def active(self, project_id: str, app_id: str) -> ConsolidationRun | None:
        return self.session.scalar(
            select(ConsolidationRun).where(
                ConsolidationRun.project_id == project_id,
                ConsolidationRun.app_id == app_id,
                ConsolidationRun.status.in_(self.NONTERMINAL),
            )
        )

    def last_succeeded(
        self, project_id: str, app_id: str
    ) -> ConsolidationRun | None:
        return self.session.scalar(
            select(ConsolidationRun)
            .where(
                ConsolidationRun.project_id == project_id,
                ConsolidationRun.app_id == app_id,
                ConsolidationRun.status == "SUCCEEDED",
            )
            .order_by(ConsolidationRun.completed_at.desc())
            .limit(1)
        )

    def last(self, project_id: str, app_id: str) -> ConsolidationRun | None:
        return self.session.scalar(
            select(ConsolidationRun)
            .where(
                ConsolidationRun.project_id == project_id,
                ConsolidationRun.app_id == app_id,
            )
            .order_by(ConsolidationRun.created_at.desc(), ConsolidationRun.id.desc())
            .limit(1)
        )

    def create(
        self,
        policy: ConsolidationPolicy,
        *,
        now: datetime,
    ) -> ConsolidationRun:
        run = ConsolidationRun(
            project_id=policy.project_id,
            app_id=policy.app_id,
            policy_id=policy.id,
            policy_version=policy.version,
            mode=policy.mode,
            status="PENDING",
            created_at=now,
            updated_at=now,
        )
        self.session.add(run)
        self.session.flush()
        return run

    def get(
        self,
        run_id: str,
        *,
        project_id: str | None = None,
        app_id: str | None = None,
    ) -> ConsolidationRun:
        statement = select(ConsolidationRun).where(
            ConsolidationRun.id == run_id
        )
        if project_id is not None:
            statement = statement.where(
                ConsolidationRun.project_id == project_id
            )
        if app_id is not None:
            statement = statement.where(ConsolidationRun.app_id == app_id)
        run = self.session.scalar(statement)
        if run is None:
            raise KeyError(run_id)
        return run

    def mark_running(
        self, run_id: str, *, scan_cutoff: datetime
    ) -> ConsolidationRun:
        run = self.get(run_id)
        run.status = "RUNNING"
        run.scan_cutoff = scan_cutoff
        run.started_at = run.started_at or scan_cutoff
        run.updated_at = scan_cutoff
        self.session.flush()
        return run

    def mark_succeeded(
        self,
        run_id: str,
        *,
        counts: Mapping[str, object],
        completed_at: datetime,
    ) -> ConsolidationRun:
        run = self.get(run_id)
        run.status = "SUCCEEDED"
        run.counts_json = _json(counts)
        run.error_code = None
        run.error_json = "{}"
        run.completed_at = completed_at
        run.updated_at = completed_at
        self.session.flush()
        return run

    def mark_failed(
        self,
        run_id: str,
        *,
        error_code: str,
        error: Mapping[str, object],
        completed_at: datetime,
    ) -> ConsolidationRun:
        run = self.get(run_id)
        run.status = "FAILED"
        run.error_code = error_code
        run.error_json = _json(dict(error))
        run.completed_at = completed_at
        run.updated_at = completed_at
        self.session.flush()
        return run


class ConsolidationProposalRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        run: ConsolidationRun,
        proposal_key: str,
        kind: str,
        source_ids: tuple[str, ...],
        canonical_id: str | None,
        score: float | None,
        evidence: Mapping[str, object],
        status: str,
    ) -> ConsolidationProposal:
        existing = self.session.scalar(
            select(ConsolidationProposal).where(
                ConsolidationProposal.run_id == run.id,
                ConsolidationProposal.proposal_key == proposal_key,
            )
        )
        if existing is not None:
            return existing
        proposal = ConsolidationProposal(
            run_id=run.id,
            project_id=run.project_id,
            app_id=run.app_id,
            proposal_key=proposal_key,
            kind=kind,
            status=status,
            source_ids_json=_json(list(source_ids)),
            canonical_memory_id=canonical_id,
            score=score,
            evidence_json=_json(dict(evidence)),
        )
        self.session.add(proposal)
        self.session.flush()
        return proposal

    def get(self, proposal_id: str) -> ConsolidationProposal:
        proposal = self.session.get(ConsolidationProposal, proposal_id)
        if proposal is None:
            raise KeyError(proposal_id)
        return proposal

    def source_ids(self, proposal: ConsolidationProposal) -> tuple[str, ...]:
        try:
            value = json.loads(proposal.source_ids_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("stored proposal source IDs are invalid") from exc
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(item, str) or not item for item in value)
        ):
            raise ValueError("stored proposal source IDs are invalid")
        return tuple(dict.fromkeys(value))

    def expected_hashes(
        self, proposal: ConsolidationProposal
    ) -> dict[str, str]:
        try:
            value = json.loads(proposal.expected_hashes_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("stored proposal hashes are invalid") from exc
        if not isinstance(value, dict) or any(
            not isinstance(key, str) or not isinstance(item, str)
            for key, item in value.items()
        ):
            raise ValueError("stored proposal hashes are invalid")
        return value

    def set_status(
        self,
        proposal: ConsolidationProposal,
        status: str,
    ) -> ConsolidationProposal:
        proposal.status = status
        proposal.updated_at = _utc_now()
        self.session.flush()
        return proposal

    def list_for_run(
        self,
        *,
        project_id: str,
        app_id: str,
        run_id: str,
        offset: int = 0,
        limit: int = 100,
    ) -> list[ConsolidationProposal]:
        if offset < 0 or limit < 1 or limit > 100:
            raise ValueError("proposal page is invalid")
        return list(
            self.session.scalars(
                select(ConsolidationProposal)
                .where(
                    ConsolidationProposal.project_id == project_id,
                    ConsolidationProposal.app_id == app_id,
                    ConsolidationProposal.run_id == run_id,
                )
                .order_by(
                    ConsolidationProposal.created_at,
                    ConsolidationProposal.id,
                )
                .offset(offset)
                .limit(limit)
            )
        )

    def list_actionable_for_run(
        self,
        run_id: str,
    ) -> list[ConsolidationProposal]:
        return list(
            self.session.scalars(
                select(ConsolidationProposal)
                .where(
                    ConsolidationProposal.run_id == run_id,
                    ConsolidationProposal.status == "PENDING",
                )
                .order_by(
                    ConsolidationProposal.created_at,
                    ConsolidationProposal.id,
                )
                .limit(1000)
            )
        )

    def count_for_run(self, run_id: str) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(ConsolidationProposal)
                .where(ConsolidationProposal.run_id == run_id)
            )
            or 0
        )

    def summary_for_run(self, run_id: str) -> dict[str, object]:
        by_kind = {
            kind: int(count)
            for kind, count in self.session.execute(
                select(ConsolidationProposal.kind, func.count())
                .where(ConsolidationProposal.run_id == run_id)
                .group_by(ConsolidationProposal.kind)
            )
        }
        by_status = {
            status: int(count)
            for status, count in self.session.execute(
                select(ConsolidationProposal.status, func.count())
                .where(ConsolidationProposal.run_id == run_id)
                .group_by(ConsolidationProposal.status)
            )
        }
        return {
            "by_kind": dict(sorted(by_kind.items())),
            "by_status": dict(sorted(by_status.items())),
            "total": sum(by_kind.values()),
        }

    def counts_for_scope(self, project_id: str, app_id: str) -> dict[str, int]:
        rows = self.session.execute(
            select(ConsolidationProposal.status, func.count())
            .where(
                ConsolidationProposal.project_id == project_id,
                ConsolidationProposal.app_id == app_id,
            )
            .group_by(ConsolidationProposal.status)
        )
        return {status: int(count) for status, count in rows}


class ConsolidationLineageRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_source(
        self,
        *,
        proposal_id: str,
        source_memory_id: str,
    ) -> ConsolidationLineage | None:
        return self.session.scalar(
            select(ConsolidationLineage).where(
                ConsolidationLineage.proposal_id == proposal_id,
                ConsolidationLineage.source_memory_id == source_memory_id,
            )
        )

    def create(
        self,
        *,
        proposal: ConsolidationProposal,
        source_memory_id: str,
        canonical_memory_id: str | None,
        action: str,
        source_content_hash: str | None,
        export_job_id: str,
        applied_at: datetime,
    ) -> ConsolidationLineage:
        existing = self.get_source(
            proposal_id=proposal.id,
            source_memory_id=source_memory_id,
        )
        if existing is not None:
            return existing
        lineage = ConsolidationLineage(
            project_id=proposal.project_id,
            app_id=proposal.app_id,
            run_id=proposal.run_id,
            proposal_id=proposal.id,
            source_memory_id=source_memory_id,
            canonical_memory_id=canonical_memory_id,
            action=action,
            source_content_hash=source_content_hash,
            export_job_id=export_job_id,
            metadata_json="{}",
            applied_at=applied_at,
        )
        self.session.add(lineage)
        self.session.flush()
        return lineage
