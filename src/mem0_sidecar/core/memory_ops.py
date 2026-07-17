import asyncio
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mem0_sidecar.core.categories import extract_category
from mem0_sidecar.core.explorer_filters import (
    EXPLORER_RECORD_HORIZON,
    ExplorerFilter,
    ExplorerQuery,
)
from mem0_sidecar.core.scope import Scope, normalize_scope, validate_scope_id
from mem0_sidecar.mem0_client.client import Mem0UpstreamError
from mem0_sidecar.observability import get_request_id
from mem0_sidecar.store.models import (
    MemoryIndex,
    MutationIntent,
    Project,
)
from mem0_sidecar.store.repositories import (
    CategoryRepository,
    EntityRepository,
    EventRepository,
    MemoryIndexRepository,
    MutationIntentFenceError,
    MutationIntentRepository,
    ProjectRepository,
)

SIDECAR_PROJECT_ID_METADATA_KEY = "_mem0_sidecar_project_id"
SIDECAR_APP_ID_METADATA_KEY = "_mem0_sidecar_app_id"
SIDECAR_MUTATION_ID_METADATA_KEY = "_mem0_sidecar_mutation_id"
_MEMORY_PATCH_FIELDS = frozenset({"text", "metadata", "expiration_date"})
_RECONCILE_SCAN_LIMIT = 5000
_HYDRATION_BUFFER = 20
_HYDRATION_CONCURRENCY = 8
_DIRTY_TRACE_SESSION_ERROR = (
    "traced memory operation requires a clean session write-set"
)


class MemoryUpstreamProtocolError(RuntimeError):
    """The upstream response does not satisfy the memory service contract."""


class MutationConflictError(RuntimeError):
    """A scoped logical mutation cannot safely be issued again."""


@dataclass(frozen=True)
class _MemoryProjectionSnapshot:
    row_id: str
    project_id: str
    mem0_memory_id: str
    user_id: str | None
    agent_id: str | None
    app_id: str | None
    run_id: str | None
    category: str | None
    entity_refs_json: str
    metadata_projection_json: str
    created_at: datetime
    updated_at: datetime


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _snapshot_memory_projection(memory: MemoryIndex) -> _MemoryProjectionSnapshot:
    return _MemoryProjectionSnapshot(
        row_id=memory.id,
        project_id=memory.project_id,
        mem0_memory_id=memory.mem0_memory_id,
        user_id=memory.user_id,
        agent_id=memory.agent_id,
        app_id=memory.app_id,
        run_id=memory.run_id,
        category=memory.category,
        entity_refs_json=memory.entity_refs_json,
        metadata_projection_json=memory.metadata_projection_json,
        created_at=_as_utc(memory.created_at),
        updated_at=_as_utc(memory.updated_at),
    )


def _append_affected_projection(
    projections_by_app: dict[str, list[_MemoryProjectionSnapshot]],
    projection: _MemoryProjectionSnapshot,
) -> None:
    app_id = projection.app_id
    if isinstance(app_id, str) and app_id:
        projections_by_app.setdefault(app_id, []).append(projection)


def _projection_matches_snapshot(
    memory: MemoryIndex,
    snapshot: _MemoryProjectionSnapshot,
) -> bool:
    return memory.deleted_at is None and _snapshot_memory_projection(memory) == snapshot


def _require_clean_trace_session(session: Session) -> None:
    if session.in_transaction() or session.new or session.dirty or session.deleted:
        raise RuntimeError(_DIRTY_TRACE_SESSION_ERROR)


def _release_read_transaction(session: Session) -> None:
    if session.new or session.dirty or session.deleted:
        raise RuntimeError("Memory read cannot release a session with pending writes")
    session.rollback()


def _require_unchanged_memory_projection(
    session: Session,
    *,
    snapshot: _MemoryProjectionSnapshot,
    app_id: str,
) -> None:
    try:
        session.expire_all()
        current = MemoryIndexRepository(session).get_memory(
            project_id=snapshot.project_id,
            mem0_memory_id=snapshot.mem0_memory_id,
            app_id=app_id,
        )
        if current is None:
            raise KeyError(snapshot.mem0_memory_id)
        if not _projection_matches_snapshot(current, snapshot):
            raise MutationConflictError(
                "Memory projection changed during upstream read"
            )
    finally:
        session.rollback()


def _append_memory_id(memory_ids: list[str], candidate: Any) -> None:
    if isinstance(candidate, str) and candidate and candidate not in memory_ids:
        memory_ids.append(candidate)


def extract_memory_ids(response: dict[str, Any]) -> list[str]:
    memory_ids: list[str] = []
    _append_memory_id(memory_ids, response.get("id"))
    _append_memory_id(memory_ids, response.get("memory_id"))
    results = response.get("results")
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            _append_memory_id(memory_ids, item.get("id"))
            _append_memory_id(memory_ids, item.get("memory_id"))
    return memory_ids


def extract_memory_id(response: dict[str, Any]) -> str:
    memory_ids = extract_memory_ids(response)
    if memory_ids:
        return memory_ids[0]
    raise MemoryUpstreamProtocolError(
        f"Could not extract memory id from response: {response!r}"
    )


def _sidecar_scope_metadata(scope: Scope) -> dict[str, str]:
    return {
        SIDECAR_PROJECT_ID_METADATA_KEY: scope.project_id,
        SIDECAR_APP_ID_METADATA_KEY: scope.app_id,
    }


def _metadata_with_sidecar_scope(
    metadata: dict[str, Any] | None,
    *,
    scope: Scope,
) -> dict[str, Any]:
    scoped_metadata = dict(metadata or {})
    scoped_metadata.update(_sidecar_scope_metadata(scope))
    return scoped_metadata


def _filters_with_sidecar_scope(
    filters: dict[str, Any] | None,
    *,
    scope: Scope,
) -> dict[str, Any]:
    scoped_filters = dict(filters or {})
    scoped_filters.update(_sidecar_scope_metadata(scope))
    return scoped_filters


def _oss_add_payload(payload: dict[str, Any], *, scope: Scope) -> dict[str, Any]:
    oss_payload = dict(payload)
    oss_payload.pop("project_id", None)
    oss_payload.pop("app_id", None)
    oss_payload["metadata"] = _metadata_with_sidecar_scope(
        payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
        scope=scope,
    )
    return oss_payload


def _canonical_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def validate_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not 1 <= len(value) <= 128 or any(
        ord(character) < 33 or ord(character) > 126 for character in value
    ):
        raise ValueError(
            "Idempotency-Key must be a visible ASCII value of 1-128 characters"
        )
    return value


def _operation_marker(
    *,
    project_id: str,
    app_id: str,
    operation: str,
    idempotency_key: str | None,
) -> str:
    logical_key = idempotency_key or secrets.token_urlsafe(32)
    return _canonical_fingerprint(
        {
            "project_id": project_id,
            "app_id": app_id,
            "operation": operation,
            "logical_key": logical_key,
        }
    )


def _expected_update_effect(patch: dict[str, Any]) -> dict[str, Any]:
    return {
        "field_fingerprints": {
            field_name: _canonical_fingerprint({"value": value})
            for field_name, value in sorted(patch.items())
        }
    }


def _update_effect_matches(
    record: dict[str, Any],
    expected_effect: Any,
) -> bool:
    if not isinstance(expected_effect, dict):
        return False
    field_fingerprints = expected_effect.get("field_fingerprints")
    if not isinstance(field_fingerprints, dict) or not field_fingerprints:
        return False
    for field_name, expected_fingerprint in field_fingerprints.items():
        if field_name == "text":
            observed = record.get("memory", record.get("text"))
        elif field_name == "metadata":
            observed = record.get("metadata")
        elif field_name == "expiration_date":
            observed = record.get("expiration_date")
        else:
            return False
        if not isinstance(expected_fingerprint, str) or (
            _canonical_fingerprint({"value": observed}) != expected_fingerprint
        ):
            return False
    return True


def _oss_search_payload(payload: dict[str, Any], *, scope: Scope) -> dict[str, Any]:
    oss_payload = dict(payload)
    oss_payload.pop("project_id", None)
    oss_payload.pop("app_id", None)
    oss_payload["filters"] = _filters_with_sidecar_scope(
        payload.get("filters") if isinstance(payload.get("filters"), dict) else None,
        scope=scope,
    )
    return oss_payload


def _result_memory_id(record: Any) -> str | None:
    if not isinstance(record, dict):
        return None
    memory_id = record.get("id") or record.get("memory_id")
    if isinstance(memory_id, str) and memory_id:
        return memory_id
    return None


def _memory_record_from_response(
    response: Any,
    *,
    expected_id: str,
) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise MemoryUpstreamProtocolError(
            "Upstream memory response must be an object"
        )
    if _result_memory_id(response) == expected_id:
        return response
    results = response.get("results")
    if isinstance(results, list):
        for item in results:
            if _result_memory_id(item) == expected_id:
                return item
    raise MemoryUpstreamProtocolError(
        f"Upstream memory response does not contain {expected_id!r}"
    )


def _record_metadata(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata")
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise MemoryUpstreamProtocolError(
            "Upstream memory record metadata must be an object or null"
        )
    return dict(metadata)


def _append_categories(categories: list[str], value: Any) -> None:
    values = value if isinstance(value, (list, tuple)) else [value]
    for candidate in values:
        if (
            isinstance(candidate, str)
            and candidate
            and candidate not in categories
        ):
            categories.append(candidate)


def _record_categories(record: dict[str, Any]) -> list[str]:
    metadata = _record_metadata(record)
    categories: list[str] = []
    _append_categories(categories, record.get("categories"))
    _append_categories(categories, metadata.get("categories"))
    for key in ("category", "custom_category", "type"):
        _append_categories(categories, metadata.get(key))
    return categories


def _normalize_memory_record(
    record: dict[str, Any],
    *,
    projection: MemoryIndex | _MemoryProjectionSnapshot | None = None,
) -> dict[str, Any]:
    memory_id = _result_memory_id(record)
    if memory_id is None:
        raise MemoryUpstreamProtocolError(
            "Upstream memory record is missing an id"
        )

    metadata = _record_metadata(record)
    categories = _record_categories(record)

    normalized: dict[str, Any] = {
        "id": memory_id,
        "memory": record.get("memory", record.get("text")),
        "metadata": metadata,
        "categories": categories,
    }
    for field in ("user_id", "agent_id", "app_id", "run_id"):
        value = record.get(field)
        if value is None and projection is not None:
            value = getattr(projection, field)
        normalized[field] = value
    for field in ("created_at", "updated_at", "expiration_date"):
        value = record.get(field)
        if value is None and projection is not None and field != "expiration_date":
            projection_value = getattr(projection, field)
            value = projection_value.isoformat() if projection_value else None
        normalized[field] = value
    return normalized


def _validate_memory_patch(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        raise ValueError("Memory update payload must not be empty")
    unknown_fields = sorted(set(payload) - _MEMORY_PATCH_FIELDS)
    if unknown_fields:
        raise ValueError(
            f"Unsupported memory update fields: {', '.join(unknown_fields)}"
        )
    if "text" in payload and (
        not isinstance(payload["text"], str) or not payload["text"].strip()
    ):
        raise ValueError("text must be a non-empty string")
    if "metadata" in payload and payload["metadata"] is not None and not isinstance(
        payload["metadata"], dict
    ):
        raise ValueError("metadata must be an object or null")
    return {key: payload[key] for key in _MEMORY_PATCH_FIELDS if key in payload}


def _history_results(response: Any) -> list[Any]:
    if isinstance(response, list):
        return response
    if not isinstance(response, dict):
        raise MemoryUpstreamProtocolError(
            "Upstream history response must be an object or list"
        )
    for key in ("results", "history"):
        if key in response:
            value = response[key]
            if not isinstance(value, list):
                raise MemoryUpstreamProtocolError(
                    f"Upstream history response {key!r} must be a list"
                )
            return value
    raise MemoryUpstreamProtocolError(
        "Upstream history response has no recognized result container"
    )


def _list_results(response: Any) -> list[Any]:
    if not isinstance(response, dict):
        raise MemoryUpstreamProtocolError(
            "Upstream list response must be an object"
        )
    for key in ("results", "memories"):
        if key in response:
            value = response[key]
            if not isinstance(value, list):
                raise MemoryUpstreamProtocolError(
                    f"Upstream list response {key!r} must be a list"
                )
            return value
    raise MemoryUpstreamProtocolError(
        "Upstream list response has no recognized result container"
    )


def _filter_search_results(
    response: dict[str, Any],
    *,
    memory_repo: MemoryIndexRepository,
    scope: Scope,
) -> dict[str, Any]:
    results = response.get("results")
    if not isinstance(results, list):
        return response

    candidate_ids: list[str] = []
    for item in results:
        _append_memory_id(candidate_ids, _result_memory_id(item))

    allowed_ids = memory_repo.list_scoped_memory_ids(
        project_id=scope.project_id,
        mem0_memory_ids=candidate_ids,
        user_id=scope.user_id,
        app_id=scope.app_id,
        agent_id=scope.agent_id,
        run_id=scope.run_id,
    )
    filtered_results = [
        item
        for item in results
        if (memory_id := _result_memory_id(item)) is not None
        and memory_id in allowed_ids
    ]
    filtered_response = dict(response)
    filtered_response["results"] = filtered_results
    filtered_response["total"] = len(filtered_results)
    return filtered_response


def _trace_filter_value(value: object) -> object:
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return dict(value)
    return value


def _query_trace_request(query: ExplorerQuery) -> dict[str, Any]:
    return {
        "match": query.match,
        "filters": [
            {
                "field": item.field,
                "operator": item.operator,
                "value": _trace_filter_value(item.value),
            }
            for item in query.filters
        ],
        "date_range": {
            "from": query.date_range.from_at.isoformat()
            if query.date_range.from_at is not None
            else None,
            "to": query.date_range.to_at.isoformat()
            if query.date_range.to_at is not None
            else None,
        },
        "page": query.page,
        "page_size": query.page_size,
        "sort": query.sort,
    }


def _exact_filter_value(item: ExplorerFilter) -> str | None:
    if item.operator == "equals" and isinstance(item.value, str):
        return item.value
    if (
        item.operator == "in"
        and isinstance(item.value, tuple)
        and len(item.value) == 1
        and isinstance(item.value[0], str)
    ):
        return item.value[0]
    return None


def _query_trace_entities(query: ExplorerQuery) -> dict[str, str]:
    if query.match != "all":
        return {}
    candidates: dict[str, set[str]] = {
        "user_id": set(),
        "agent_id": set(),
        "run_id": set(),
    }
    for item in query.filters:
        values = candidates.get(item.field)
        if values is None:
            continue
        if (value := _exact_filter_value(item)) is not None:
            values.add(value)
    return {
        field_name: next(iter(values))
        for field_name, values in candidates.items()
        if len(values) == 1
    }


def _hydrated_record_matches_projection(
    raw_record: dict[str, Any],
    normalized_record: dict[str, Any],
    projection: MemoryIndex | _MemoryProjectionSnapshot,
) -> bool:
    if not all(
        normalized_record.get(field_name) == getattr(projection, field_name)
        for field_name in ("user_id", "agent_id", "app_id", "run_id")
    ):
        return False

    raw_project_id = raw_record.get("project_id")
    if raw_project_id is not None:
        try:
            normalized_project_id = validate_scope_id(
                raw_project_id, field_name="project_id"
            )
        except ValueError:
            return False
        if normalized_project_id != projection.project_id:
            return False

    metadata = normalized_record.get("metadata")
    if not isinstance(metadata, dict):
        return False
    has_project_marker = SIDECAR_PROJECT_ID_METADATA_KEY in metadata
    has_app_marker = SIDECAR_APP_ID_METADATA_KEY in metadata
    if not has_project_marker and not has_app_marker:
        return True
    if not has_project_marker or not has_app_marker:
        return False
    try:
        marker_project_id = validate_scope_id(
            metadata[SIDECAR_PROJECT_ID_METADATA_KEY],
            field_name="project_id",
        )
        marker_app_id = validate_scope_id(
            metadata[SIDECAR_APP_ID_METADATA_KEY],
            field_name="app_id",
        )
    except ValueError:
        return False
    return (
        marker_project_id == projection.project_id
        and marker_app_id == projection.app_id
    )


def _persist_failed_trace(
    session: Session,
    *,
    event_id: str,
    error: dict[str, Any],
) -> None:
    session.rollback()
    EventRepository(session).mark_failed(event_id, error=error)
    session.commit()


def _event_payload(event: Any) -> dict[str, Any]:
    return {
        "id": event.id,
        "operation": event.operation,
        "status": event.status,
        "subject_type": event.subject_type,
        "subject_id": event.subject_id,
    }


def _error_payload(exc: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error_type": type(exc).__name__,
        "message": str(exc),
    }
    if request_id := get_request_id():
        payload["request_id"] = request_id
    if isinstance(exc, Mem0UpstreamError):
        payload["upstream_method"] = exc.method
        payload["upstream_path"] = exc.path
        payload["upstream_status_code"] = exc.status_code
        if exc.response_text is not None:
            payload["upstream_response_text"] = exc.response_text[:1000]
    return payload


def _validate_get_response(memory_id: str, response: Any) -> dict[str, Any]:
    if not isinstance(response, dict) or memory_id not in extract_memory_ids(response):
        raise KeyError(memory_id)
    return response


def _is_upstream_not_found(exc: Exception) -> bool:
    return isinstance(exc, Mem0UpstreamError) and exc.status_code == 404


def _is_ambiguous_upstream_failure(exc: Exception) -> bool:
    if type(exc) is not Mem0UpstreamError:
        return True
    try:
        return object.__getattribute__(exc, "outcome_unknown") is not False
    except BaseException:
        return True


def _effective_request_app_id(
    session: Session,
    *,
    project_id: str,
    request_app_id: str | None,
) -> str:
    if request_app_id:
        return request_app_id

    project = session.get(Project, project_id)
    if project is not None and project.default_app_id:
        return project.default_app_id

    return project_id


def _memory_delete_request(
    *,
    project_id: str,
    memory_id: str,
    memory: MemoryIndex | None,
    request_app_id: str | None = None,
) -> dict[str, Any]:
    if memory is None:
        scope = normalize_scope(
            project_id=project_id,
            user_id=None,
            app_id=request_app_id,
            agent_id=None,
            run_id=None,
        )
        request = {"memory_id": memory_id}
        request.update(scope.as_filter_dict())
        return request

    request = {
        "memory_id": memory.mem0_memory_id,
        "user_id": memory.user_id,
        "agent_id": memory.agent_id,
        "app_id": memory.app_id,
        "run_id": memory.run_id,
    }
    return {key: value for key, value in request.items() if value}


class MemoryService:
    def __init__(self, *, session: Session, mem0: Any) -> None:
        self.session = session
        self.mem0 = mem0

    async def _reuse_add_intent(
        self,
        intent: MutationIntent,
        *,
        request_fingerprint: str,
    ) -> dict[str, Any]:
        intent_repo = MutationIntentRepository(self.session)
        if (
            intent_repo.payload(intent).get("request_fingerprint")
            != request_fingerprint
        ):
            raise MutationConflictError(
                "Idempotency-Key was already used with a different request"
            )
        if intent.status == "COMPLETED":
            result = intent_repo.result(intent)
            if result:
                return result
        lease_expires_at = intent.lease_expires_at
        if lease_expires_at is not None:
            now = datetime.now(UTC)
            if lease_expires_at.tzinfo is None:
                now = now.replace(tzinfo=None)
            if lease_expires_at > now:
                raise MutationConflictError(
                    "Idempotent add is already in progress; no second add was issued"
                )
        await self.recover_pending_mutations(
            project_id=intent.project_id,
            app_id=intent.app_id,
        )
        self.session.expire_all()
        intent = intent_repo.get(intent.id)
        if intent.status == "COMPLETED":
            result = intent_repo.result(intent)
            if result:
                return result
        raise MutationConflictError(
            "Idempotent add outcome remains unresolved; no second add was issued"
        )

    def _record_intent_failure(
        self,
        project_id: str,
        intent_id: str,
        expected_attempt_count: int,
        exc: BaseException,
        *,
        outcome_unknown: bool,
        mark_event_failed: bool,
    ) -> None:
        self.session.rollback()
        try:
            ProjectRepository(self.session).lock_for_mutation(project_id)
            intent_repo = MutationIntentRepository(self.session)
            intent = intent_repo.require_active_attempt(
                intent_id,
                expected_attempt_count,
            )
            error = (
                _error_payload(exc)
                if isinstance(exc, Exception)
                else {"message": "Mutation interrupted; recovery required"}
            )
            if outcome_unknown:
                intent_repo.mark_unresolved(intent_id, error=error)
            else:
                intent_repo.fail(intent_id, error=error)
            if mark_event_failed:
                EventRepository(self.session).mark_failed(
                    intent.event_id,
                    error=error,
                )
            self.session.commit()
        except MutationIntentFenceError:
            self.session.rollback()
        except BaseException:
            self.session.rollback()

    async def recover_pending_mutations(
        self,
        *,
        project_id: str,
        app_id: str,
    ) -> dict[str, int]:
        """Observe bounded incomplete intents before the next scoped mutation."""

        ProjectRepository(self.session).lock_for_mutation(project_id)
        initial_repo = MutationIntentRepository(self.session)
        intent_ids = [
            intent.id
            for intent in initial_repo.list_recoverable(project_id, app_id)
        ]
        if not intent_ids:
            blockers = initial_repo.list_blocking(project_id, app_id)
            if not blockers:
                return {"recovered": 0, "failed": 0}
            self.session.rollback()
            if any(intent.status == "EXHAUSTED" for intent in blockers):
                raise MutationConflictError(
                    "Scoped mutation recovery is exhausted and remains unresolved"
                )
            if any(intent.status == "ACTIVE" for intent in blockers):
                raise MutationConflictError(
                    "Scoped mutation recovery is already in progress"
                )
            raise MutationConflictError(
                "Scoped mutation recovery remains unresolved"
            )
        self.session.rollback()
        recovered = 0
        failed = 0
        for intent_id in intent_ids:
            claimed = False
            attempt_token: int | None = None
            try:
                ProjectRepository(self.session).lock_for_mutation(project_id)
                intent_repo = MutationIntentRepository(self.session)
                intent = intent_repo.get(intent_id)
                if intent.status not in intent_repo.RECOVERABLE_STATUSES:
                    self.session.rollback()
                    continue
                claimed = intent_repo.claim_recovery(intent)
                attempt_token = intent.attempt_count
                operation = intent.operation
                self.session.commit()
                if not claimed:
                    failed += 1
                    continue

                if operation == "memory.add":
                    await self._recover_add_intent(intent_id, attempt_token)
                elif operation == "memory.update":
                    await self._recover_update_intent(intent_id, attempt_token)
                elif operation in {"memory.delete", "entity.delete"}:
                    await self._recover_delete_intent(intent_id, attempt_token)
                elif operation == "memory.reconcile":
                    ProjectRepository(self.session).lock_for_mutation(project_id)
                    intent_repo = MutationIntentRepository(self.session)
                    intent = intent_repo.require_active_attempt(
                        intent_id,
                        attempt_token,
                    )
                    error = {
                        "message": (
                            "Interrupted reconciliation must be started again"
                        )
                    }
                    EventRepository(self.session).mark_failed(
                        intent.event_id,
                        error=error,
                    )
                    intent_repo.fail(intent_id, error=error)
                else:
                    raise RuntimeError("Unsupported durable mutation intent")
                final_status = MutationIntentRepository(self.session).get(
                    intent_id
                ).status
                self.session.commit()
                if final_status == "COMPLETED":
                    recovered += 1
                elif final_status in {"FAILED", "PARTIAL", "EXHAUSTED"}:
                    failed += 1
            except BaseException as exc:
                self.session.rollback()
                if (
                    claimed
                    and attempt_token is not None
                    and not isinstance(exc, MutationIntentFenceError)
                ):
                    try:
                        ProjectRepository(self.session).lock_for_mutation(project_id)
                        intent_repo = MutationIntentRepository(self.session)
                        intent_repo.require_active_attempt(
                            intent_id,
                            attempt_token,
                        )
                        intent = intent_repo.mark_unresolved(
                            intent_id,
                            error=(
                                _error_payload(exc)
                                if isinstance(exc, Exception)
                                else {
                                    "message": (
                                        "Mutation observation interrupted; "
                                        "recovery remains unresolved"
                                    )
                                }
                            ),
                        )
                        EventRepository(self.session).mark_failed(
                            intent.event_id,
                            error={
                                "message": (
                                    "Mutation observation failed; "
                                    "recovery remains unresolved"
                                )
                            },
                        )
                        self.session.commit()
                    except MutationIntentFenceError:
                        self.session.rollback()
                    except BaseException:
                        self.session.rollback()
                if not isinstance(exc, Exception):
                    raise
                failed += 1

        self.session.rollback()
        ProjectRepository(self.session).lock_for_mutation(project_id)
        blockers = MutationIntentRepository(self.session).list_blocking(
            project_id,
            app_id,
        )
        blocking_statuses = {intent.status for intent in blockers}
        if blockers:
            self.session.rollback()
            if "EXHAUSTED" in blocking_statuses:
                raise MutationConflictError(
                    "Scoped mutation recovery is exhausted and remains unresolved"
                )
            if "ACTIVE" in blocking_statuses:
                raise MutationConflictError(
                    "Scoped mutation recovery is already in progress"
                )
            raise MutationConflictError(
                "Scoped mutation recovery remains unresolved"
            )
        return {"recovered": recovered, "failed": failed}

    async def _recover_add_intent(
        self,
        intent_id: str,
        expected_attempt_count: int,
    ) -> None:
        intent_repo = MutationIntentRepository(self.session)
        intent = intent_repo.get(intent_id)
        payload = intent_repo.payload(intent)
        marker = payload.get("mutation_id")
        if not isinstance(marker, str) or not marker:
            raise RuntimeError("Add recovery marker is unavailable")
        project_id = intent.project_id
        app_id = intent.app_id
        category = payload.get("category")
        self.session.rollback()

        async def marked_records() -> list[dict[str, Any]]:
            response = await self.mem0.list_memories(
                {"top_k": _RECONCILE_SCAN_LIMIT, "show_expired": True}
            )
            records: list[dict[str, Any]] = []
            for item in _list_results(response):
                if not isinstance(item, dict):
                    continue
                metadata = item.get("metadata")
                if (
                    isinstance(metadata, dict)
                    and metadata.get(SIDECAR_MUTATION_ID_METADATA_KEY) == marker
                    and metadata.get(SIDECAR_PROJECT_ID_METADATA_KEY)
                    == project_id
                    and metadata.get(SIDECAR_APP_ID_METADATA_KEY) == app_id
                ):
                    records.append(item)
            return records

        records = await marked_records()
        if not records:
            raise MutationConflictError(
                "Add outcome remains unknown; retry with the original "
                "Idempotency-Key after recovery"
            )

        normalized = sorted(
            (_normalize_memory_record(record) for record in records),
            key=lambda item: item["id"],
        )
        ProjectRepository(self.session).lock_for_mutation(project_id)
        intent_repo = MutationIntentRepository(self.session)
        intent = intent_repo.require_active_attempt(
            intent_id,
            expected_attempt_count,
        )
        memory_repo = MemoryIndexRepository(self.session)
        affected_projections: list[_MemoryProjectionSnapshot] = []
        for item in normalized:
            metadata = dict(item["metadata"])
            metadata.pop(SIDECAR_PROJECT_ID_METADATA_KEY, None)
            metadata.pop(SIDECAR_APP_ID_METADATA_KEY, None)
            metadata.pop(SIDECAR_MUTATION_ID_METADATA_KEY, None)
            existing = memory_repo.get_memory(
                project_id=project_id,
                mem0_memory_id=item["id"],
                app_id=app_id,
                include_deleted=True,
            )
            if existing is not None:
                affected_projections.append(_snapshot_memory_projection(existing))
            indexed = memory_repo.upsert_memory(
                project_id=project_id,
                mem0_memory_id=item["id"],
                user_id=item["user_id"],
                agent_id=item["agent_id"],
                app_id=app_id,
                run_id=item["run_id"],
                category=category if isinstance(category, str) else None,
                metadata=metadata,
            )
            affected_projections.append(_snapshot_memory_projection(indexed))
        intent_repo.add_targets(
            intent_id,
            [item["id"] for item in normalized],
        )
        for target in intent_repo.targets(intent_id):
            intent_repo.mark_target_succeeded(target)
        EntityRepository(self.session).refresh_affected_memories(
            project_id,
            app_id,
            affected_projections,
        )
        event = EventRepository(self.session).get(intent.event_id)
        event.subject_id = normalized[0]["id"]
        memory_response: dict[str, Any] = (
            normalized[0] if len(normalized) == 1 else {"results": normalized}
        )
        EventRepository(self.session).mark_succeeded(
            event.id,
            response=memory_response,
        )
        result = intent_repo.sanitize_payload(
            project_id,
            {"memory": memory_response, "event": _event_payload(event)},
        )
        intent_repo.complete(intent_id, result=result)

    async def _recover_update_intent(
        self,
        intent_id: str,
        expected_attempt_count: int,
    ) -> None:
        intent_repo = MutationIntentRepository(self.session)
        intent = intent_repo.get(intent_id)
        payload = intent_repo.payload(intent)
        expected_effect = payload.get("expected_effect")
        targets = intent_repo.targets(intent_id)
        if len(targets) != 1:
            raise RuntimeError("Update recovery target is invalid")
        memory_id = targets[0].memory_id
        project_id = intent.project_id
        app_id = intent.app_id
        self.session.rollback()
        try:
            response = await self.mem0.get_memory(memory_id)
            record = _memory_record_from_response(
                response,
                expected_id=memory_id,
            )
        except Exception as exc:
            if not _is_upstream_not_found(exc):
                raise
            ProjectRepository(self.session).lock_for_mutation(project_id)
            intent_repo = MutationIntentRepository(self.session)
            intent = intent_repo.require_active_attempt(
                intent_id,
                expected_attempt_count,
            )
            error = {
                "message": "Updated memory is absent; outcome remains unknown",
                "retry_required": True,
            }
            EventRepository(self.session).mark_failed(
                intent.event_id,
                error=error,
            )
            intent_repo.mark_unresolved(intent_id, error=error)
            return

        ProjectRepository(self.session).lock_for_mutation(project_id)
        intent_repo = MutationIntentRepository(self.session)
        intent = intent_repo.require_active_attempt(
            intent_id,
            expected_attempt_count,
        )
        if not _update_effect_matches(record, expected_effect):
            error = {
                "message": "Observed memory does not match requested update",
                "retry_required": True,
            }
            EventRepository(self.session).mark_failed(
                intent.event_id,
                error=error,
            )
            intent_repo.mark_unresolved(intent_id, error=error)
            return
        memory_repo = MemoryIndexRepository(self.session)
        projection = memory_repo.get_memory(
            project_id=project_id,
            mem0_memory_id=memory_id,
            app_id=app_id,
        )
        normalized = _normalize_memory_record(record, projection=projection)
        categories = normalized["categories"]
        affected_projections = (
            [_snapshot_memory_projection(projection)]
            if projection is not None
            else []
        )
        indexed = memory_repo.upsert_memory(
            project_id=project_id,
            mem0_memory_id=memory_id,
            user_id=normalized["user_id"],
            agent_id=normalized["agent_id"],
            app_id=app_id,
            run_id=normalized["run_id"],
            category=categories[0] if categories else None,
            metadata=normalized["metadata"],
        )
        affected_projections.append(_snapshot_memory_projection(indexed))
        target = intent_repo.targets(intent_id)[0]
        intent_repo.mark_target_succeeded(target)
        EntityRepository(self.session).refresh_affected_memories(
            project_id,
            app_id,
            affected_projections,
        )
        result = {"id": memory_id, "recovered": True}
        EventRepository(self.session).mark_succeeded(intent.event_id, response=result)
        intent_repo.complete(intent_id, result=result)

    async def _recover_delete_intent(
        self,
        intent_id: str,
        expected_attempt_count: int,
    ) -> None:
        intent_repo = MutationIntentRepository(self.session)
        intent = intent_repo.get(intent_id)
        project_id = intent.project_id
        app_id = intent.app_id
        operation = intent.operation
        pending_ids = [
            target.memory_id
            for target in intent_repo.targets(intent_id, pending_only=True)
        ]
        total_targets = len(intent_repo.targets(intent_id))
        self.session.rollback()

        missing_ids: list[str] = []
        present_ids: list[str] = []
        for offset in range(0, len(pending_ids), 50):
            for memory_id in pending_ids[offset : offset + 50]:
                try:
                    response = await self.mem0.get_memory(memory_id)
                    _memory_record_from_response(
                        response,
                        expected_id=memory_id,
                    )
                except Exception as exc:
                    if not _is_upstream_not_found(exc):
                        raise
                    missing_ids.append(memory_id)
                    continue
                present_ids.append(memory_id)
            ProjectRepository(self.session).lock_for_mutation(project_id)
            MutationIntentRepository(self.session).renew_active_attempt(
                intent_id,
                expected_attempt_count,
            )
            self.session.commit()

        ProjectRepository(self.session).lock_for_mutation(project_id)
        intent_repo = MutationIntentRepository(self.session)
        intent = intent_repo.require_active_attempt(
            intent_id,
            expected_attempt_count,
        )
        targets = {
            target.memory_id: target for target in intent_repo.targets(intent_id)
        }
        memory_repo = MemoryIndexRepository(self.session)

        affected_projections: list[_MemoryProjectionSnapshot] = []
        for memory_id in missing_ids:
            memory = memory_repo.get_memory(
                project_id=project_id,
                mem0_memory_id=memory_id,
                app_id=app_id,
            )
            if memory is not None:
                affected_projections.append(_snapshot_memory_projection(memory))
            memory_repo.delete_memory(
                project_id=project_id,
                mem0_memory_id=memory_id,
            )
            intent_repo.mark_target_succeeded(targets[memory_id])
        EntityRepository(self.session).refresh_affected_memories(
            project_id,
            app_id,
            affected_projections,
        )

        successful_count = sum(
            target.status == "COMPLETED" for target in targets.values()
        )
        if present_ids:
            if operation == "entity.delete" and successful_count:
                for memory_id in present_ids:
                    intent_repo.mark_target_failed(
                        targets[memory_id],
                        {"message": "Memory still exists upstream"},
                    )
                failed_count = sum(
                    target.status == "FAILED" for target in targets.values()
                )
                result = {
                    "status": "PARTIAL",
                    "requested_count": total_targets,
                    "deleted_count": successful_count,
                    "failed_count": failed_count,
                    "retry_required": True,
                }
                EventRepository(self.session).mark_failed(
                    intent.event_id,
                    error=result,
                )
                intent_repo.fail(
                    intent_id,
                    status="PARTIAL",
                    error=result,
                    result=result,
                )
                return
            error = {
                "message": "Delete target still exists upstream",
                "retry_required": True,
            }
            EventRepository(self.session).mark_failed(
                intent.event_id,
                error=error,
            )
            intent_repo.mark_unresolved(intent_id, error=error)
            return
        failed_count = sum(
            target.status == "FAILED" for target in targets.values()
        )
        if operation == "entity.delete" and failed_count:
            status = "PARTIAL" if successful_count else "FAILED"
            result = {
                "status": status,
                "requested_count": total_targets,
                "deleted_count": successful_count,
                "failed_count": failed_count,
                "retry_required": True,
            }
            EventRepository(self.session).mark_failed(
                intent.event_id,
                error=result,
            )
            intent_repo.fail(
                intent_id,
                status=status,
                error=result,
                result=result,
            )
            return
        result = {
            "recovered": True,
            "count": successful_count,
            "success": True,
        }
        EventRepository(self.session).mark_succeeded(intent.event_id, response=result)
        intent_repo.complete(intent_id, result=result)

    async def add_memory(
        self,
        *,
        project_id: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        _require_clean_trace_session(self.session)
        idempotency_key = validate_idempotency_key(idempotency_key)
        scope = normalize_scope(
            project_id=project_id,
            user_id=payload.get("user_id"),
            app_id=payload.get("app_id"),
            agent_id=payload.get("agent_id"),
            run_id=payload.get("run_id"),
        )
        metadata = dict(payload.get("metadata") or {})
        category_names = {
            category.name
            for category in CategoryRepository(self.session).list_project_categories(
                project_id
            )
            if category.enabled
        }
        category = extract_category(metadata, category_names)
        oss_payload = _oss_add_payload(payload, scope=scope)
        oss_payload.update(
            {
                key: value
                for key, value in {
                    "user_id": scope.user_id,
                    "agent_id": scope.agent_id,
                    "run_id": scope.run_id,
                }.items()
                if value
            }
        )
        event_request = {
            **oss_payload,
            "metadata": dict(oss_payload["metadata"]),
        }
        request_fingerprint = _canonical_fingerprint(event_request)
        mutation_id = _operation_marker(
            project_id=project_id,
            app_id=scope.app_id,
            operation="memory.add",
            idempotency_key=idempotency_key,
        )
        intent_repo = MutationIntentRepository(self.session)
        existing = (
            intent_repo.find_by_operation_key(
                project_id=project_id,
                app_id=scope.app_id,
                operation="memory.add",
                operation_key=mutation_id,
            )
            if idempotency_key is not None
            else None
        )
        if existing is not None:
            return await self._reuse_add_intent(
                existing,
                request_fingerprint=request_fingerprint,
            )

        recovery = await self.recover_pending_mutations(
            project_id=project_id,
            app_id=scope.app_id,
        )
        if recovery["failed"]:
            raise MutationConflictError(
                "Scoped mutation recovery remains unresolved; no add was issued"
            )
        oss_payload["metadata"][SIDECAR_MUTATION_ID_METADATA_KEY] = mutation_id

        event_repo = EventRepository(self.session)
        event = event_repo.create_event(
            project_id=project_id,
            app_id=scope.app_id,
            user_id=scope.user_id,
            agent_id=scope.agent_id,
            run_id=scope.run_id,
            operation="memory.add",
            request=event_request,
            subject_type="memory",
            correlation_id=get_request_id(),
        )
        intent_payload = {
            "mutation_id": mutation_id,
            "request_fingerprint": request_fingerprint,
            "upstream_payload": oss_payload,
            "category": category,
        }
        sanitized_payload = intent_repo.sanitize_payload(
            project_id,
            intent_payload,
        )
        intent_payload["replay_safe"] = (
            sanitized_payload.get("upstream_payload") == oss_payload
            and sanitized_payload.get("category") == category
        )
        try:
            intent = intent_repo.create(
                project_id=project_id,
                app_id=scope.app_id,
                event_id=event.id,
                operation="memory.add",
                payload=intent_payload,
                operation_key=mutation_id,
            )
        except IntegrityError:
            self.session.rollback()
            intent_repo = MutationIntentRepository(self.session)
            existing = intent_repo.find_by_operation_key(
                project_id=project_id,
                app_id=scope.app_id,
                operation="memory.add",
                operation_key=mutation_id,
            )
            if existing is None:
                raise MutationConflictError(
                    "Idempotent add is already in progress; no second add was issued"
                ) from None
            return await self._reuse_add_intent(
                existing,
                request_fingerprint=request_fingerprint,
            )
        event_id = event.id
        intent_id = intent.id
        attempt_token = intent.attempt_count
        self.session.commit()
        upstream_attempted = False
        upstream_completed = False
        try:
            upstream_attempted = True
            memory_response = await self.mem0.add_memory(oss_payload)
            upstream_completed = True
            memory_ids = extract_memory_ids(memory_response)
            if not memory_ids:
                raise MemoryUpstreamProtocolError(
                    f"Could not extract memory id from response: {memory_response!r}"
                )
            ProjectRepository(self.session).lock_for_mutation(project_id)
            intent_repo = MutationIntentRepository(self.session)
            intent_repo.require_active_attempt(intent_id, attempt_token)
            memory_repo = MemoryIndexRepository(self.session)
            affected_projections: list[_MemoryProjectionSnapshot] = []
            for memory_id in memory_ids:
                existing = memory_repo.get_memory(
                    project_id=project_id,
                    mem0_memory_id=memory_id,
                    app_id=scope.app_id,
                    include_deleted=True,
                )
                if existing is not None:
                    affected_projections.append(_snapshot_memory_projection(existing))
                indexed = memory_repo.upsert_memory(
                    project_id=project_id,
                    mem0_memory_id=memory_id,
                    user_id=scope.user_id,
                    app_id=scope.app_id,
                    agent_id=scope.agent_id,
                    run_id=scope.run_id,
                    category=category,
                    metadata=metadata,
                )
                affected_projections.append(_snapshot_memory_projection(indexed))
            for target in intent_repo.add_targets(intent_id, memory_ids):
                intent_repo.mark_target_succeeded(target)
            EntityRepository(self.session).refresh_affected_memories(
                project_id,
                scope.app_id,
                affected_projections,
            )
            event_repo = EventRepository(self.session)
            event = event_repo.get(event_id)
            event.subject_id = memory_ids[0]
            event_repo.mark_succeeded(event_id, response=memory_response)
            result = intent_repo.sanitize_payload(
                project_id,
                {"memory": memory_response, "event": _event_payload(event)},
            )
            intent_repo.complete(intent_id, result=result)
            self.session.commit()
            return result
        except BaseException as exc:
            self._record_intent_failure(
                project_id,
                intent_id,
                attempt_token,
                exc,
                outcome_unknown=(
                    not isinstance(exc, Exception)
                    or upstream_completed
                    or (
                        upstream_attempted
                        and _is_ambiguous_upstream_failure(exc)
                    )
                ),
                mark_event_failed=isinstance(exc, Exception),
            )
            if isinstance(exc, MutationIntentFenceError):
                raise MutationConflictError(
                    "Durable mutation attempt was superseded"
                ) from exc
            raise

    async def search_memories(
        self,
        *,
        project_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        _require_clean_trace_session(self.session)
        scope = normalize_scope(
            project_id=project_id,
            user_id=payload.get("user_id"),
            app_id=payload.get("app_id"),
            agent_id=payload.get("agent_id"),
            run_id=payload.get("run_id"),
        )
        oss_payload = _oss_search_payload(payload, scope=scope)
        oss_payload.update(
            {
                key: value
                for key, value in {
                    "user_id": scope.user_id,
                    "agent_id": scope.agent_id,
                    "run_id": scope.run_id,
                }.items()
                if value
            }
        )
        event_repo = EventRepository(self.session)
        event = event_repo.create_event(
            project_id=project_id,
            app_id=scope.app_id,
            user_id=scope.user_id,
            agent_id=scope.agent_id,
            run_id=scope.run_id,
            operation="memory.search",
            request=oss_payload,
            subject_type="memory",
            correlation_id=get_request_id(),
        )
        self.session.commit()
        try:
            response = await self.mem0.search_memories(oss_payload)
            filtered_response = _filter_search_results(
                response,
                memory_repo=MemoryIndexRepository(self.session),
                scope=scope,
            )
            event_repo.mark_succeeded(event.id, response=filtered_response)
            self.session.commit()
            return filtered_response
        except Exception as exc:
            _persist_failed_trace(
                self.session,
                event_id=event.id,
                error=_error_payload(exc),
            )
            raise

    async def query_memories(
        self,
        *,
        project_id: str,
        app_id: str,
        query: ExplorerQuery,
    ) -> dict[str, Any]:
        _require_clean_trace_session(self.session)
        trace_entities = _query_trace_entities(query)
        event_repo = EventRepository(self.session)
        event = event_repo.create_event(
            project_id=project_id,
            app_id=app_id,
            user_id=trace_entities.get("user_id"),
            agent_id=trace_entities.get("agent_id"),
            run_id=trace_entities.get("run_id"),
            operation="memory.list",
            request=_query_trace_request(query),
            subject_type="memory",
            correlation_id=get_request_id(),
        )
        event_id = event.id
        self.session.commit()
        memory_repo = MemoryIndexRepository(self.session)
        logical_offset = (query.page - 1) * query.page_size
        logical_end = logical_offset + query.page_size
        hydration_batch_size = min(
            query.page_size + _HYDRATION_BUFFER,
            EXPLORER_RECORD_HORIZON,
        )
        window_limit = hydration_batch_size
        fetched_ids: set[str] = set()
        observed: dict[str, _MemoryProjectionSnapshot] = {}
        hydrated: dict[
            str,
            tuple[_MemoryProjectionSnapshot, dict[str, Any]],
        ] = {}
        stale: dict[str, _MemoryProjectionSnapshot] = {}
        observation_changed = False

        def candidate_page() -> tuple[int, int, list[_MemoryProjectionSnapshot]]:
            nonlocal observation_changed
            try:
                page = memory_repo.query_project_memories(
                    project_id,
                    app_id,
                    query,
                    window_offset=0,
                    window_limit=window_limit,
                )
                snapshots = [
                    _snapshot_memory_projection(memory) for memory in page.items
                ]
                for snapshot in snapshots:
                    previous = observed.setdefault(
                        snapshot.mem0_memory_id,
                        snapshot,
                    )
                    if previous != snapshot:
                        observation_changed = True
                return page.total, page.scan_count, snapshots
            finally:
                self.session.rollback()

        async def hydrate(
            snapshot: _MemoryProjectionSnapshot,
        ) -> tuple[_MemoryProjectionSnapshot, dict[str, Any] | None]:
            try:
                response = await self.mem0.get_memory(snapshot.mem0_memory_id)
                record = _memory_record_from_response(
                    response,
                    expected_id=snapshot.mem0_memory_id,
                )
                normalized = _normalize_memory_record(
                    record,
                    projection=snapshot,
                )
                if not _hydrated_record_matches_projection(
                    record,
                    normalized,
                    snapshot,
                ):
                    return snapshot, None
                return snapshot, normalized
            except Exception as exc:
                if _is_upstream_not_found(exc) or isinstance(
                    exc,
                    (
                        KeyError,
                        MemoryUpstreamProtocolError,
                        TypeError,
                        ValueError,
                    ),
                ):
                    return snapshot, None
                raise

        def finalize_query(
            *,
            forced_conflict: str | None = None,
        ) -> dict[str, Any]:
            ProjectRepository(self.session).lock_for_mutation(project_id)
            self.session.expire_all()

            conflict = forced_conflict
            stale_marked = 0
            if stale:
                current_stale = {
                    memory.mem0_memory_id: memory
                    for memory in memory_repo.list_memories_by_ids(
                        project_id=project_id,
                        app_id=app_id,
                        mem0_memory_ids=stale,
                        include_deleted=True,
                    )
                }
                unchanged_stale = {
                    memory_id: snapshot
                    for memory_id, snapshot in stale.items()
                    if (
                        (memory := current_stale.get(memory_id)) is not None
                        and _projection_matches_snapshot(memory, snapshot)
                    )
                }
                if len(unchanged_stale) != len(stale):
                    conflict = (
                        "Memory projection changed during hydration; retry the query"
                    )
                if unchanged_stale:
                    latest_stale_version = max(
                        (
                            snapshot.updated_at.replace(tzinfo=UTC)
                            if snapshot.updated_at.tzinfo is None
                            else snapshot.updated_at.astimezone(UTC)
                        )
                        for snapshot in unchanged_stale.values()
                    )
                    stale_marked = memory_repo.mark_stale_if_unchanged(
                        project_id=project_id,
                        app_id=app_id,
                        mem0_memory_ids=unchanged_stale,
                        updated_at_lte=latest_stale_version,
                        expected_updated_at={
                            memory_id: snapshot.updated_at
                            for memory_id, snapshot in unchanged_stale.items()
                        },
                    )
                    if stale_marked:
                        EntityRepository(self.session).refresh_affected_memories(
                            project_id,
                            app_id,
                            unchanged_stale.values(),
                        )
                    if stale_marked != len(unchanged_stale):
                        conflict = (
                            "Memory projection changed during hydration; "
                            "retry the query"
                        )

            self.session.expire_all()
            if hydrated:
                current_hydrated = {
                    memory.mem0_memory_id: memory
                    for memory in memory_repo.list_memories_by_ids(
                        project_id=project_id,
                        app_id=app_id,
                        mem0_memory_ids=hydrated,
                        include_deleted=True,
                    )
                }
                if any(
                    (
                        (memory := current_hydrated.get(memory_id)) is None
                        or not _projection_matches_snapshot(memory, snapshot)
                    )
                    for memory_id, (snapshot, _record) in hydrated.items()
                ):
                    conflict = (
                        "Memory projection changed during hydration; retry the query"
                    )

            final_page = memory_repo.query_project_memories(
                project_id,
                app_id,
                query,
                window_offset=0,
                window_limit=window_limit,
            )
            final_candidates = [
                _snapshot_memory_projection(memory) for memory in final_page.items
            ]
            needed_candidates = final_candidates[:logical_end]
            if observation_changed or any(
                snapshot.mem0_memory_id not in hydrated
                or hydrated[snapshot.mem0_memory_id][0] != snapshot
                for snapshot in needed_candidates
            ):
                conflict = (
                    "Memory projection order changed during hydration; retry the query"
                )

            if conflict is not None:
                raise MutationConflictError(conflict)

            ordered_results = [
                hydrated[snapshot.mem0_memory_id][1]
                for snapshot in final_candidates
                if snapshot.mem0_memory_id in hydrated
            ]
            response = {
                "results": ordered_results[logical_offset:logical_end],
                "total": final_page.total,
                "page": query.page,
                "page_size": query.page_size,
                "scan_count": final_page.scan_count,
                "stale_skipped": stale_marked,
            }
            event_repo.mark_succeeded(event_id, response=response)
            self.session.commit()
            return response

        try:
            semaphore = asyncio.Semaphore(_HYDRATION_CONCURRENCY)

            async def bounded_hydrate(
                snapshot: _MemoryProjectionSnapshot,
            ) -> tuple[_MemoryProjectionSnapshot, dict[str, Any] | None]:
                async with semaphore:
                    return await hydrate(snapshot)

            while True:
                candidate_total, _scan_count, candidates = candidate_page()
                logical_candidates = [
                    snapshot
                    for snapshot in candidates
                    if snapshot.mem0_memory_id not in stale
                ]
                page_is_full = len(logical_candidates) >= logical_end and all(
                    snapshot.mem0_memory_id in hydrated
                    for snapshot in logical_candidates[:logical_end]
                )
                active_set_is_exhausted = candidate_total <= len(candidates) and all(
                    snapshot.mem0_memory_id in hydrated
                    or snapshot.mem0_memory_id in stale
                    for snapshot in candidates
                )
                if page_is_full or active_set_is_exhausted:
                    return finalize_query()

                remaining_budget = EXPLORER_RECORD_HORIZON - len(fetched_ids)
                if remaining_budget <= 0:
                    return finalize_query(
                        forced_conflict=(
                            "Memory hydration horizon reached; retry the query"
                        )
                    )

                unseen_candidates = [
                    snapshot
                    for snapshot in candidates
                    if snapshot.mem0_memory_id not in fetched_ids
                ]
                if not unseen_candidates:
                    if window_limit >= EXPLORER_RECORD_HORIZON:
                        return finalize_query(
                            forced_conflict=(
                                "Memory hydration horizon reached; retry the query"
                            )
                        )
                    window_limit = min(
                        window_limit + hydration_batch_size,
                        EXPLORER_RECORD_HORIZON,
                    )
                    continue

                hydration_batch = unseen_candidates[
                    : min(hydration_batch_size, remaining_budget)
                ]
                fetched_ids.update(
                    snapshot.mem0_memory_id for snapshot in hydration_batch
                )
                hydration_results = await asyncio.gather(
                    *(bounded_hydrate(snapshot) for snapshot in hydration_batch)
                )
                for snapshot, normalized in hydration_results:
                    memory_id = snapshot.mem0_memory_id
                    if normalized is None:
                        stale[memory_id] = snapshot
                    else:
                        hydrated[memory_id] = (snapshot, normalized)
        except MutationConflictError as exc:
            event_repo.mark_failed(event_id, error=_error_payload(exc))
            self.session.commit()
            raise
        except Exception as exc:
            _persist_failed_trace(
                self.session,
                event_id=event_id,
                error=_error_payload(exc),
            )
            raise

    async def get_memory(
        self,
        *,
        project_id: str,
        memory_id: str,
        request_app_id: str | None = None,
    ) -> dict[str, Any]:
        _require_clean_trace_session(self.session)
        effective_app_id = _effective_request_app_id(
            self.session,
            project_id=project_id,
            request_app_id=request_app_id,
        )
        memory_repo = MemoryIndexRepository(self.session)
        memory = memory_repo.get_memory(
            project_id=project_id,
            mem0_memory_id=memory_id,
            app_id=effective_app_id,
        )
        if memory is None:
            self.session.rollback()
            raise KeyError(memory_id)
        projection_before = _snapshot_memory_projection(memory)
        _release_read_transaction(self.session)

        try:
            response = await self.mem0.get_memory(memory_id)
        except Exception as exc:
            if _is_upstream_not_found(exc):
                raise KeyError(memory_id) from exc
            raise
        validated_response = _validate_get_response(memory_id, response)
        _require_unchanged_memory_projection(
            self.session,
            snapshot=projection_before,
            app_id=effective_app_id,
        )
        return validated_response

    async def update_memory(
        self,
        *,
        project_id: str,
        memory_id: str,
        request_app_id: str | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        patch = _validate_memory_patch(payload)
        effective_app_id = _effective_request_app_id(
            self.session,
            project_id=project_id,
            request_app_id=request_app_id,
        )
        await self.recover_pending_mutations(
            project_id=project_id,
            app_id=effective_app_id,
        )
        memory_repo = MemoryIndexRepository(self.session)
        memory = memory_repo.get_memory(
            project_id=project_id,
            mem0_memory_id=memory_id,
            app_id=effective_app_id,
        )
        if memory is None:
            self.session.rollback()
            raise KeyError(memory_id)

        if "metadata" in patch:
            scope = normalize_scope(
                project_id=project_id,
                user_id=memory.user_id,
                app_id=memory.app_id,
                agent_id=memory.agent_id,
                run_id=memory.run_id,
            )
            patch["metadata"] = _metadata_with_sidecar_scope(
                patch["metadata"] if isinstance(patch["metadata"], dict) else None,
                scope=scope,
            )

        event_repo = EventRepository(self.session)
        event = event_repo.create_event(
            project_id=project_id,
            app_id=effective_app_id,
            user_id=memory.user_id,
            agent_id=memory.agent_id,
            run_id=memory.run_id,
            operation="memory.update",
            request=patch,
            subject_type="memory",
            subject_id=memory_id,
        )
        intent = MutationIntentRepository(self.session).create(
            project_id=project_id,
            app_id=effective_app_id,
            event_id=event.id,
            operation="memory.update",
            payload={
                "memory_id": memory_id,
                "expected_effect": _expected_update_effect(patch),
            },
            memory_ids=[memory_id],
        )
        projection_before = _snapshot_memory_projection(memory)
        event_id = event.id
        intent_id = intent.id
        attempt_token = intent.attempt_count
        self.session.commit()
        upstream_attempted = False
        upstream_completed = False
        try:
            try:
                upstream_attempted = True
                update_response = await self.mem0.update_memory(memory_id, patch)
                upstream_completed = True
            except ValueError as exc:
                raise MemoryUpstreamProtocolError(
                    "Upstream memory update response could not be decoded"
                ) from exc
            ProjectRepository(self.session).lock_for_mutation(project_id)
            MutationIntentRepository(self.session).renew_active_attempt(
                intent_id,
                attempt_token,
            )
            self.session.commit()
            try:
                refresh_response = await self.mem0.get_memory(memory_id)
            except ValueError as exc:
                raise MemoryUpstreamProtocolError(
                    "Upstream memory refresh response could not be decoded"
                ) from exc
            record = _memory_record_from_response(
                refresh_response,
                expected_id=memory_id,
            )
            normalized = _normalize_memory_record(
                record,
                projection=projection_before,
            )
            ProjectRepository(self.session).lock_for_mutation(project_id)
            intent_repo = MutationIntentRepository(self.session)
            intent_repo.require_active_attempt(intent_id, attempt_token)
            current_memory = memory_repo.get_memory(
                project_id=project_id,
                mem0_memory_id=memory_id,
                app_id=effective_app_id,
            )
            if current_memory is None or not _projection_matches_snapshot(
                current_memory,
                projection_before,
            ):
                raise MutationConflictError(
                    "Memory projection changed during upstream update"
                )
            metadata = normalized["metadata"]
            categories = normalized["categories"]
            indexed = memory_repo.upsert_memory(
                project_id=project_id,
                mem0_memory_id=memory_id,
                user_id=normalized["user_id"],
                agent_id=normalized["agent_id"],
                app_id=effective_app_id,
                run_id=normalized["run_id"],
                category=categories[0] if categories else None,
                metadata=metadata,
            )
            EntityRepository(self.session).refresh_affected_memories(
                project_id,
                effective_app_id,
                [projection_before, _snapshot_memory_projection(indexed)],
            )
            event_repo = EventRepository(self.session)
            event = event_repo.get(event_id)
            event_repo.mark_succeeded(event_id, response=update_response)
            intent_repo.mark_target_succeeded(intent_repo.targets(intent_id)[0])
            result = {"memory": normalized, "event": _event_payload(event)}
            intent_repo.complete(intent_id, result=result)
            self.session.commit()
            return result
        except BaseException as exc:
            if (
                _is_upstream_not_found(exc)
                and not upstream_completed
                and not isinstance(exc, MutationIntentFenceError)
            ):
                self.session.rollback()
                ProjectRepository(self.session).lock_for_mutation(project_id)
                intent_repo = MutationIntentRepository(self.session)
                intent_repo.require_active_attempt(intent_id, attempt_token)
                current_memory = memory_repo.get_memory(
                    project_id=project_id,
                    mem0_memory_id=memory_id,
                    app_id=effective_app_id,
                )
                if current_memory is None or not _projection_matches_snapshot(
                    current_memory,
                    projection_before,
                ):
                    raise MutationConflictError(
                        "Memory projection changed during upstream update"
                    ) from exc
                memory_repo.mark_stale(project_id, [memory_id])
                EntityRepository(self.session).refresh_affected_memories(
                    project_id,
                    effective_app_id,
                    [projection_before],
                )
                event_repo = EventRepository(self.session)
                event_repo.mark_failed(event_id, error=_error_payload(exc))
                intent_repo.mark_target_failed(
                    intent_repo.targets(intent_id)[0],
                    _error_payload(exc),
                )
                intent_repo.fail(intent_id, error=_error_payload(exc))
                self.session.commit()
                raise KeyError(memory_id) from exc
            self._record_intent_failure(
                project_id,
                intent_id,
                attempt_token,
                exc,
                outcome_unknown=(
                    not isinstance(exc, Exception)
                    or upstream_completed
                    or (
                        upstream_attempted
                        and _is_ambiguous_upstream_failure(exc)
                    )
                ),
                mark_event_failed=isinstance(exc, Exception),
            )
            if isinstance(exc, MutationIntentFenceError):
                raise MutationConflictError(
                    "Durable mutation attempt was superseded"
                ) from exc
            raise

    async def get_memory_history(
        self,
        *,
        project_id: str,
        memory_id: str,
        request_app_id: str | None,
    ) -> dict[str, Any]:
        _require_clean_trace_session(self.session)
        effective_app_id = _effective_request_app_id(
            self.session,
            project_id=project_id,
            request_app_id=request_app_id,
        )
        memory = MemoryIndexRepository(self.session).get_memory(
            project_id=project_id,
            mem0_memory_id=memory_id,
            app_id=effective_app_id,
        )
        if memory is None:
            self.session.rollback()
            raise KeyError(memory_id)
        projection_before = _snapshot_memory_projection(memory)
        _release_read_transaction(self.session)
        try:
            response = await self.mem0.get_memory_history(memory_id)
        except ValueError as exc:
            raise MemoryUpstreamProtocolError(
                "Upstream memory history response could not be decoded"
            ) from exc
        except Exception as exc:
            if _is_upstream_not_found(exc):
                raise KeyError(memory_id) from exc
            raise
        results = _history_results(response)
        _require_unchanged_memory_projection(
            self.session,
            snapshot=projection_before,
            app_id=effective_app_id,
        )
        return {"results": results}

    async def reconcile_memories(
        self,
        *,
        project_id: str,
        app_id: str,
        adopt_unscoped: bool,
        allow_adopt_unscoped: bool,
        default_project_id: str,
    ) -> dict[str, int]:
        if adopt_unscoped and not allow_adopt_unscoped:
            raise ValueError("Unscoped memory adoption is disabled at runtime")
        if adopt_unscoped and project_id != default_project_id:
            raise ValueError(
                "Unscoped memories may only be adopted by the default project"
            )
        await self.recover_pending_mutations(project_id=project_id, app_id=app_id)
        event_repo = EventRepository(self.session)
        event = event_repo.create_event(
            project_id=project_id,
            app_id=app_id,
            operation="memory.reconcile",
            request={
                "app_id": app_id,
                "adopt_unscoped": adopt_unscoped,
            },
            subject_type="memory",
        )
        intent = MutationIntentRepository(self.session).create(
            project_id=project_id,
            app_id=app_id,
            event_id=event.id,
            operation="memory.reconcile",
            payload={"adopt_unscoped": adopt_unscoped},
        )
        event_id = event.id
        intent_id = intent.id
        attempt_token = intent.attempt_count
        self.session.commit()
        scan_cutoff = datetime.now(UTC)
        try:
            response = await self.mem0.list_memories(
                {"top_k": _RECONCILE_SCAN_LIMIT, "show_expired": True}
            )
        except ValueError as exc:
            error = MemoryUpstreamProtocolError(
                "Upstream memory list response could not be decoded"
            )
            self._record_intent_failure(
                project_id,
                intent_id,
                attempt_token,
                error,
                outcome_unknown=False,
                mark_event_failed=True,
            )
            raise error from exc
        except BaseException as exc:
            self._record_intent_failure(
                project_id,
                intent_id,
                attempt_token,
                exc,
                outcome_unknown=False,
                mark_event_failed=True,
            )
            raise

        try:
            records = _list_results(response)
            response_total = response.get("total")
            if "total" in response and (
                isinstance(response_total, bool)
                or not isinstance(response_total, int)
                or response_total < len(records)
            ):
                raise MemoryUpstreamProtocolError(
                    "Upstream list response total is invalid"
                )
            normalized_records: list[dict[str, Any]] = []
            for index, item in enumerate(records):
                if not isinstance(item, dict):
                    raise MemoryUpstreamProtocolError(
                        f"Upstream memory record {index} must be an object"
                    )
                try:
                    normalized_records.append(_normalize_memory_record(item))
                except (MemoryUpstreamProtocolError, TypeError, ValueError) as exc:
                    raise MemoryUpstreamProtocolError(
                        f"Upstream memory record {index} is malformed"
                    ) from exc

            accepted_ids: set[str] = set()
            indexed = 0
            skipped_unscoped = 0
            skipped_other_scope = 0
            for offset in range(0, len(normalized_records), 200):
                ProjectRepository(self.session).lock_for_mutation(project_id)
                intent_repo = MutationIntentRepository(self.session)
                intent_repo.require_active_attempt(intent_id, attempt_token)
                memory_repo = MemoryIndexRepository(self.session)
                entity_repo = EntityRepository(self.session)
                affected_projections: dict[
                    str, list[_MemoryProjectionSnapshot]
                ] = {}
                for normalized in normalized_records[offset : offset + 200]:
                    metadata = normalized["metadata"]
                    scoped_project_id = metadata.get(
                        SIDECAR_PROJECT_ID_METADATA_KEY
                    )
                    scoped_app_id = metadata.get(SIDECAR_APP_ID_METADATA_KEY)
                    has_project_scope = isinstance(scoped_project_id, str)
                    has_app_scope = isinstance(scoped_app_id, str)
                    is_unscoped = not has_project_scope and not has_app_scope
                    is_matching_scope = (
                        scoped_project_id == project_id and scoped_app_id == app_id
                    )
                    if is_unscoped:
                        if not adopt_unscoped:
                            skipped_unscoped += 1
                            continue
                    elif not is_matching_scope:
                        skipped_other_scope += 1
                        continue

                    categories = normalized["categories"]
                    memory_id = normalized["id"]
                    existing = memory_repo.get_memory(
                        project_id=project_id,
                        mem0_memory_id=memory_id,
                        include_deleted=True,
                    )
                    if (
                        existing is not None
                        and _as_utc(existing.updated_at) > scan_cutoff
                    ):
                        accepted_ids.add(memory_id)
                        indexed += 1
                        continue
                    existing_projection = (
                        _snapshot_memory_projection(existing)
                        if existing is not None
                        else None
                    )
                    if is_unscoped:
                        claim = memory_repo.claim_memory(
                            project_id=project_id,
                            mem0_memory_id=memory_id,
                            user_id=normalized["user_id"],
                            agent_id=normalized["agent_id"],
                            app_id=app_id,
                            run_id=normalized["run_id"],
                            category=categories[0] if categories else None,
                            metadata=metadata,
                        )
                        if claim.status == "conflict":
                            skipped_other_scope += 1
                            continue
                        indexed_memory = claim.memory
                    else:
                        indexed_memory = memory_repo.upsert_memory(
                            project_id=project_id,
                            mem0_memory_id=memory_id,
                            user_id=normalized["user_id"],
                            agent_id=normalized["agent_id"],
                            app_id=app_id,
                            run_id=normalized["run_id"],
                            category=categories[0] if categories else None,
                            metadata=metadata,
                        )
                    if indexed_memory is None:
                        raise RuntimeError(
                            "Reconciled memory projection is unavailable"
                        )
                    if existing_projection is not None:
                        _append_affected_projection(
                            affected_projections,
                            existing_projection,
                        )
                    _append_affected_projection(
                        affected_projections,
                        _snapshot_memory_projection(indexed_memory),
                    )
                    accepted_ids.add(memory_id)
                    indexed += 1
                for affected_app_id, projections in affected_projections.items():
                    entity_repo.refresh_affected_memories(
                        project_id,
                        affected_app_id,
                        projections,
                    )
                intent_repo.renew_active_attempt(intent_id, attempt_token)
                self.session.commit()

            truncated = len(records) >= _RECONCILE_SCAN_LIMIT or (
                isinstance(response_total, int) and response_total > len(records)
            )
            stale_marked = 0
            if not truncated:
                after_created_at: datetime | None = None
                after_memory_id: str | None = None
                while True:
                    ProjectRepository(self.session).lock_for_mutation(project_id)
                    intent_repo = MutationIntentRepository(self.session)
                    intent_repo.require_active_attempt(intent_id, attempt_token)
                    memory_repo = MemoryIndexRepository(self.session)
                    candidates = memory_repo.list_reconcile_stale_candidates(
                        project_id=project_id,
                        app_id=app_id,
                        updated_at_lte=scan_cutoff,
                        after_created_at=after_created_at,
                        after_memory_id=after_memory_id,
                        limit=200,
                    )
                    if not candidates:
                        self.session.rollback()
                        break
                    stale_candidates = [
                        memory
                        for memory in candidates
                        if memory.mem0_memory_id not in accepted_ids
                    ]
                    stale_snapshots = [
                        _snapshot_memory_projection(memory)
                        for memory in stale_candidates
                    ]
                    stale_marked += memory_repo.mark_stale_if_unchanged(
                        project_id=project_id,
                        app_id=app_id,
                        mem0_memory_ids=[
                            memory.mem0_memory_id for memory in stale_candidates
                        ],
                        updated_at_lte=scan_cutoff,
                        expected_updated_at={
                            memory.mem0_memory_id: memory.updated_at
                            for memory in stale_candidates
                        },
                    )
                    EntityRepository(self.session).refresh_affected_memories(
                        project_id,
                        app_id,
                        stale_snapshots,
                    )
                    cursor = candidates[-1]
                    after_created_at = cursor.created_at
                    after_memory_id = cursor.mem0_memory_id
                    intent_repo.renew_active_attempt(intent_id, attempt_token)
                    self.session.commit()

            result = {
                "scanned": len(records),
                "indexed": indexed,
                "skipped_unscoped": skipped_unscoped,
                "skipped_other_scope": skipped_other_scope,
                "stale_marked": stale_marked,
            }
            ProjectRepository(self.session).lock_for_mutation(project_id)
            intent_repo = MutationIntentRepository(self.session)
            intent_repo.require_active_attempt(intent_id, attempt_token)
            event_repo = EventRepository(self.session)
            event_repo.mark_succeeded(event_id, response=result)
            intent_repo.complete(intent_id, result=result)
            self.session.commit()
            return result
        except BaseException as exc:
            self._record_intent_failure(
                project_id,
                intent_id,
                attempt_token,
                exc,
                outcome_unknown=False,
                mark_event_failed=True,
            )
            if isinstance(exc, MutationIntentFenceError):
                raise MutationConflictError(
                    "Durable reconciliation attempt was superseded"
                ) from exc
            raise

    async def delete_memory(
        self,
        *,
        project_id: str,
        memory_id: str,
        request_app_id: str | None = None,
    ) -> dict[str, Any]:
        effective_app_id = _effective_request_app_id(
            self.session,
            project_id=project_id,
            request_app_id=request_app_id,
        )
        await self.recover_pending_mutations(
            project_id=project_id,
            app_id=effective_app_id,
        )
        memory_repo = MemoryIndexRepository(self.session)
        memory = memory_repo.get_memory(
            project_id=project_id,
            mem0_memory_id=memory_id,
            app_id=effective_app_id,
        )
        if memory is None:
            event_repo = EventRepository(self.session)
            event = event_repo.create_event(
                project_id=project_id,
                app_id=effective_app_id,
                operation="memory.delete",
                request=_memory_delete_request(
                    project_id=project_id,
                    memory_id=memory_id,
                    memory=None,
                    request_app_id=effective_app_id,
                ),
                subject_type="memory",
                subject_id=memory_id,
            )
            event_repo.mark_failed(
                event.id,
                error={
                    "message": (
                        f"Memory {memory_id!r} not found for project {project_id!r}"
                    )
                },
            )
            self.session.commit()
            raise KeyError(memory_id)

        event_repo = EventRepository(self.session)
        event = event_repo.create_event(
            project_id=project_id,
            app_id=effective_app_id,
            user_id=memory.user_id,
            agent_id=memory.agent_id,
            run_id=memory.run_id,
            operation="memory.delete",
            request=_memory_delete_request(
                project_id=project_id,
                memory_id=memory_id,
                memory=memory,
            ),
            subject_type="memory",
            subject_id=memory_id,
        )
        intent = MutationIntentRepository(self.session).create(
            project_id=project_id,
            app_id=effective_app_id,
            event_id=event.id,
            operation="memory.delete",
            payload={"memory_id": memory_id},
            memory_ids=[memory_id],
        )
        projection_before = _snapshot_memory_projection(memory)
        event_id = event.id
        intent_id = intent.id
        attempt_token = intent.attempt_count
        self.session.commit()
        upstream_attempted = False
        upstream_completed = False
        try:
            upstream_attempted = True
            response = await self.mem0.delete_memory(memory_id)
            upstream_completed = True
            ProjectRepository(self.session).lock_for_mutation(project_id)
            intent_repo = MutationIntentRepository(self.session)
            intent_repo.require_active_attempt(intent_id, attempt_token)
            current_memory = memory_repo.get_memory(
                project_id=project_id,
                mem0_memory_id=memory_id,
                app_id=effective_app_id,
            )
            if current_memory is None or not _projection_matches_snapshot(
                current_memory,
                projection_before,
            ):
                raise MutationConflictError(
                    "Memory projection changed during upstream delete"
                )
            memory_repo.delete_memory(
                project_id=project_id,
                mem0_memory_id=memory_id,
            )
            EntityRepository(self.session).refresh_affected_memories(
                project_id,
                effective_app_id,
                [projection_before],
            )
            event_repo = EventRepository(self.session)
            event = event_repo.get(event_id)
            event_repo.mark_succeeded(event_id, response=response)
            intent_repo.mark_target_succeeded(intent_repo.targets(intent_id)[0])
            result = {"memory": response, "event": _event_payload(event)}
            intent_repo.complete(intent_id, result=result)
            self.session.commit()
            return result
        except BaseException as exc:
            if _is_upstream_not_found(exc) and not isinstance(
                exc,
                MutationIntentFenceError,
            ):
                self.session.rollback()
                ProjectRepository(self.session).lock_for_mutation(project_id)
                intent_repo = MutationIntentRepository(self.session)
                intent_repo.require_active_attempt(intent_id, attempt_token)
                current_memory = memory_repo.get_memory(
                    project_id=project_id,
                    mem0_memory_id=memory_id,
                    app_id=effective_app_id,
                )
                if current_memory is None or not _projection_matches_snapshot(
                    current_memory,
                    projection_before,
                ):
                    raise MutationConflictError(
                        "Memory projection changed during upstream delete"
                    ) from exc
                memory_repo.delete_memory(
                    project_id=project_id,
                    mem0_memory_id=memory_id,
                )
                EntityRepository(self.session).refresh_affected_memories(
                    project_id,
                    effective_app_id,
                    [projection_before],
                )
                event_repo = EventRepository(self.session)
                event_repo.mark_failed(event_id, error=_error_payload(exc))
                intent_repo.mark_target_succeeded(intent_repo.targets(intent_id)[0])
                intent_repo.complete(intent_id, result={"missing": True})
                self.session.commit()
                raise KeyError(memory_id) from exc
            self._record_intent_failure(
                project_id,
                intent_id,
                attempt_token,
                exc,
                outcome_unknown=(
                    not isinstance(exc, Exception)
                    or upstream_completed
                    or (
                        upstream_attempted
                        and _is_ambiguous_upstream_failure(exc)
                    )
                ),
                mark_event_failed=isinstance(exc, Exception),
            )
            if isinstance(exc, MutationIntentFenceError):
                raise MutationConflictError(
                    "Durable mutation attempt was superseded"
                ) from exc
            raise
