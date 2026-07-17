from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from mem0_sidecar.core.explorer_filters import (
    ExplorerDateRange,
    ExplorerFilter,
    parse_explorer_query,
)
from mem0_sidecar.core.scope import validate_scope_id
from mem0_sidecar.mem0_client.client import Mem0UpstreamError
from mem0_sidecar.observability import get_request_id
from mem0_sidecar.store.models import Entity
from mem0_sidecar.store.repositories import (
    EntityRepository,
    EventRepository,
    MemoryIndexRepository,
    MutationIntentFenceError,
    MutationIntentRepository,
    ProjectRepository,
)

_ENTITY_TYPES = frozenset({"user", "agent", "app", "run"})
_ENTITY_FILTER_FIELDS = {"entity_type", "user_id", "agent_id", "app_id", "run_id"}
_FILTER_ENTITY_TYPES = {
    "user_id": "user",
    "agent_id": "agent",
    "app_id": "app",
    "run_id": "run",
}


@dataclass(frozen=True)
class EntityQuery:
    match: str
    filters: tuple[ExplorerFilter, ...]
    date_range: ExplorerDateRange
    page: int
    page_size: int
    entity_type: str


def _normalize_entity_type(value: object) -> str:
    if type(value) is not str:
        raise ValueError("Unsupported entity type")
    if value != value.strip():
        raise ValueError("Unsupported entity type")
    normalized = value.lower()
    if normalized not in _ENTITY_TYPES:
        raise ValueError("Unsupported entity type")
    return normalized


def _normalize_type_filter(value: object) -> object:
    if isinstance(value, (list, tuple)):
        return [_normalize_entity_type(item) for item in value]
    return _normalize_entity_type(value)


def _portable_id(value: object, *, field_name: str) -> str:
    normalized = validate_scope_id(value, field_name=field_name)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    return normalized


def validate_entity_identity(
    entity_type: object,
    entity_id: object,
) -> tuple[str, str]:
    normalized_type = _normalize_entity_type(entity_type)
    normalized_id = _portable_id(
        entity_id,
        field_name=f"{normalized_type}_id",
    )
    return normalized_type, normalized_id


def _normalize_id_filter(value: object, *, field_name: str) -> object:
    if isinstance(value, (list, tuple)):
        return [
            _portable_id(item, field_name=field_name)
            for item in value
        ]
    return _portable_id(value, field_name=field_name)


def parse_entity_query(payload: Mapping[str, object]) -> EntityQuery:
    if not isinstance(payload, Mapping):
        raise ValueError("query must be an object")
    entity_type = _normalize_entity_type(payload.get("entity_type", "user"))
    normalized_payload = dict(payload)
    normalized_payload.pop("entity_type", None)
    raw_filters = normalized_payload.get("filters", [])
    if isinstance(raw_filters, list):
        filters: list[object] = []
        for index, raw_filter in enumerate(raw_filters):
            if not isinstance(raw_filter, Mapping):
                filters.append(raw_filter)
                continue
            field = raw_filter.get("field")
            if type(field) is not str:
                raise ValueError(f"filters[{index}].field is not allowed")
            if field == "entity_type":
                normalized_filter = dict(raw_filter)
                normalized_filter["value"] = _normalize_type_filter(
                    raw_filter.get("value")
                )
                filters.append(normalized_filter)
            elif field in _FILTER_ENTITY_TYPES:
                normalized_filter = dict(raw_filter)
                normalized_filter["value"] = _normalize_id_filter(
                    raw_filter.get("value"),
                    field_name=field,
                )
                filters.append(normalized_filter)
            else:
                filters.append(raw_filter)
        normalized_payload["filters"] = filters
    explorer_query = parse_explorer_query(
        normalized_payload,
        allowed_fields=_ENTITY_FILTER_FIELDS,
    )
    return EntityQuery(
        match=explorer_query.match,
        filters=explorer_query.filters,
        date_range=explorer_query.date_range,
        page=explorer_query.page,
        page_size=explorer_query.page_size,
        entity_type=entity_type,
    )


def _validated_query(query: EntityQuery) -> EntityQuery:
    if not isinstance(query, EntityQuery):
        raise ValueError("query must be an EntityQuery")
    return parse_entity_query(
        {
            "entity_type": query.entity_type,
            "match": query.match,
            "filters": [
                {
                    "field": item.field,
                    "operator": item.operator,
                    "value": item.value,
                }
                for item in query.filters
            ],
            "date_range": {
                "from": (
                    query.date_range.from_at.isoformat()
                    if query.date_range.from_at is not None
                    else None
                ),
                "to": (
                    query.date_range.to_at.isoformat()
                    if query.date_range.to_at is not None
                    else None
                ),
            },
            "page": query.page,
            "page_size": query.page_size,
        }
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _datetime_payload(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value is not None else None


def _entity_payload(entity: Entity) -> dict[str, Any]:
    return {
        "id": entity.id,
        "type": entity.entity_type,
        "entity_id": entity.entity_id,
        "display_name": entity.display_name,
        "memory_count": entity.memory_count,
        "last_seen_at": _datetime_payload(entity.last_seen_at),
        "updated_at": _datetime_payload(entity.updated_at),
    }


def _value_predicate(column: Any, item: ExplorerFilter) -> Any:
    if item.operator == "equals":
        return column == item.value
    if item.operator == "not_equals":
        return column != item.value
    if item.operator == "in":
        return column.in_(item.value)
    raise ValueError(f"Unsupported entity filter operator: {item.operator}")


def _filter_predicate(item: ExplorerFilter) -> Any:
    if item.field == "entity_type":
        return _value_predicate(Entity.entity_type, item)
    entity_type = _FILTER_ENTITY_TYPES.get(item.field)
    if entity_type is None:
        raise ValueError(f"Unsupported entity filter field: {item.field}")
    return and_(
        Entity.entity_type == entity_type,
        _value_predicate(Entity.entity_id, item),
    )


def _safe_upstream_status_code(exc: Exception) -> int | None:
    if type(exc) is not Mem0UpstreamError:
        return None
    try:
        status_code = object.__getattribute__(exc, "status_code")
    except BaseException:
        return None
    if type(status_code) is not int or not 100 <= status_code <= 599:
        return None
    return status_code


def _safe_outcome_unknown(exc: Exception) -> bool:
    if type(exc) is not Mem0UpstreamError:
        return True
    try:
        outcome_unknown = object.__getattribute__(exc, "outcome_unknown")
    except BaseException:
        return True
    return outcome_unknown is not False


def _safe_delete_error(exc: Exception) -> dict[str, Any]:
    safe: dict[str, Any] = {
        "error_type": (
            "Mem0UpstreamError"
            if isinstance(exc, Mem0UpstreamError)
            else "UpstreamDeleteError"
        ),
        "message": "Upstream memory deletion failed",
    }
    if (status_code := _safe_upstream_status_code(exc)) is not None:
        safe["upstream_status_code"] = status_code
    return safe


class EntityService:
    def __init__(self, *, session: Session, mem0: Any) -> None:
        self.session = session
        self.mem0 = mem0

    def query_entities(
        self,
        project_id: str,
        app_id: str,
        query: EntityQuery,
    ) -> dict[str, Any]:
        project_id = _portable_id(project_id, field_name="project_id")
        app_id = _portable_id(app_id, field_name="app_id")
        query = _validated_query(query)
        predicates: list[Any] = [
            Entity.project_id == project_id,
            Entity.app_id == app_id,
            Entity.entity_type == query.entity_type,
        ]
        if query.date_range.from_at is not None:
            predicates.append(Entity.last_seen_at >= query.date_range.from_at)
        if query.date_range.to_at is not None:
            predicates.append(Entity.last_seen_at <= query.date_range.to_at)
        filter_predicates = [_filter_predicate(item) for item in query.filters]
        if filter_predicates:
            predicates.append(
                and_(*filter_predicates)
                if query.match == "all"
                else or_(*filter_predicates)
            )

        total = int(
            self.session.scalar(
                select(func.count()).select_from(Entity).where(*predicates)
            )
            or 0
        )
        entities = list(
            self.session.scalars(
                select(Entity)
                .where(*predicates)
                .order_by(
                    Entity.last_seen_at.is_(None),
                    Entity.last_seen_at.desc(),
                    Entity.entity_id.asc(),
                    Entity.id.asc(),
                )
                .offset((query.page - 1) * query.page_size)
                .limit(query.page_size)
            )
        )
        return {
            "results": [_entity_payload(entity) for entity in entities],
            "page": query.page,
            "page_size": query.page_size,
            "total": total,
        }

    def get_entity(
        self,
        project_id: str,
        app_id: str,
        entity_type: str,
        entity_id: str,
    ) -> dict[str, Any]:
        normalized_type, entity_id = validate_entity_identity(
            entity_type,
            entity_id,
        )
        project_id = _portable_id(project_id, field_name="project_id")
        app_id = _portable_id(app_id, field_name="app_id")
        entity = EntityRepository(self.session).get_project_entity(
            project_id,
            app_id,
            normalized_type,
            entity_id,
        )
        return _entity_payload(entity)

    def rebuild_entities(self, project_id: str, app_id: str) -> dict[str, int]:
        project_id = _portable_id(project_id, field_name="project_id")
        app_id = _portable_id(app_id, field_name="app_id")
        entities = EntityRepository(self.session).rebuild_project_entities(
            project_id,
            app_id,
        )
        return {"entities": len(entities)}

    async def delete_entity(
        self,
        project_id: str,
        app_id: str,
        entity_type: str,
        entity_id: str,
    ) -> dict[str, Any]:
        normalized_type, entity_id = validate_entity_identity(
            entity_type,
            entity_id,
        )
        project_id = _portable_id(project_id, field_name="project_id")
        app_id = _portable_id(app_id, field_name="app_id")
        from mem0_sidecar.core.memory_ops import MemoryService, MutationConflictError

        recovery_service = MemoryService(session=self.session, mem0=self.mem0)
        await recovery_service.recover_pending_mutations(
            project_id=project_id,
            app_id=app_id,
        )
        ProjectRepository(self.session).lock_for_mutation(project_id)

        entity_repo = EntityRepository(self.session)
        entity = entity_repo.get_project_entity(
            project_id,
            app_id,
            normalized_type,
            entity_id,
        )
        memory_ids = entity_repo.list_entity_memory_ids(
            project_id,
            app_id,
            normalized_type,
            entity_id,
        )
        requested_count = len(memory_ids)
        event_repo = EventRepository(self.session)
        canonical_entities = {
            "user_id": entity_id if normalized_type == "user" else None,
            "agent_id": entity_id if normalized_type == "agent" else None,
            "run_id": entity_id if normalized_type == "run" else None,
        }
        event = event_repo.create_event(
            project_id=project_id,
            app_id=app_id,
            user_id=canonical_entities["user_id"],
            agent_id=canonical_entities["agent_id"],
            run_id=canonical_entities["run_id"],
            operation="entity.delete",
            request={
                "app_id": app_id,
                "entity_type": normalized_type,
                "entity_id": entity_id,
                "projected_count": entity.memory_count,
            },
            subject_type="entity",
            subject_id=entity_id,
            correlation_id=get_request_id(),
        )
        intent = MutationIntentRepository(self.session).create(
            project_id=project_id,
            app_id=app_id,
            event_id=event.id,
            operation="entity.delete",
            payload={
                "entity_type": normalized_type,
                "entity_id": entity_id,
            },
            memory_ids=memory_ids,
        )
        expected_updated_at = {
            memory.mem0_memory_id: _as_utc(memory.updated_at)
            for memory in MemoryIndexRepository(self.session).list_memories_by_ids(
                project_id=project_id,
                app_id=app_id,
                mem0_memory_ids=memory_ids,
            )
        }
        event_id = event.id
        intent_id = intent.id
        attempt_token = intent.attempt_count
        self.session.commit()

        upstream_attempted = False
        upstream_completed = False
        upstream_effect_observed = False
        try:
            failed: list[dict[str, Any]] = []
            deleted_count = 0
            for offset in range(0, len(memory_ids), 50):
                outcomes: list[tuple[str, str, dict[str, Any] | None]] = []
                pending_error: BaseException | None = None
                for memory_id in memory_ids[offset : offset + 50]:
                    try:
                        upstream_attempted = True
                        await self.mem0.delete_memory(memory_id)
                        upstream_effect_observed = True
                        outcomes.append(("deleted", memory_id, None))
                    except BaseException as exc:
                        if not isinstance(exc, Exception) or _safe_outcome_unknown(exc):
                            pending_error = exc
                            break
                        if (
                            isinstance(exc, Mem0UpstreamError)
                            and _safe_upstream_status_code(exc) == 404
                        ):
                            upstream_effect_observed = True
                            outcomes.append(("deleted", memory_id, None))
                            continue
                        safe_error = _safe_delete_error(exc)
                        outcomes.append(("failed", memory_id, safe_error))

                if outcomes:
                    ProjectRepository(self.session).lock_for_mutation(project_id)
                    intent_repo = MutationIntentRepository(self.session)
                    intent_repo.require_active_attempt(intent_id, attempt_token)
                    targets = {
                        target.memory_id: target
                        for target in intent_repo.targets(intent_id)
                    }
                    memory_repo = MemoryIndexRepository(self.session)
                    affected_memories: list[Any] = []
                    for outcome, memory_id, safe_error in outcomes:
                        target = targets[memory_id]
                        if outcome == "failed":
                            assert safe_error is not None
                            failed.append({"id": memory_id, "error": safe_error})
                            intent_repo.mark_target_failed(target, safe_error)
                            continue
                        current = memory_repo.get_memory(
                            project_id=project_id,
                            mem0_memory_id=memory_id,
                            app_id=app_id,
                        )
                        if (
                            current is None
                            or _as_utc(current.updated_at)
                            != expected_updated_at.get(memory_id)
                        ):
                            raise MutationConflictError(
                                "Entity memory projection changed during delete"
                            )
                        deleted_memory = memory_repo.delete_memory(
                            project_id=project_id,
                            mem0_memory_id=memory_id,
                        )
                        if deleted_memory is not None:
                            affected_memories.append(deleted_memory)
                        intent_repo.mark_target_succeeded(target)
                        deleted_count += 1
                    entity_repo.refresh_affected_memories(
                        project_id,
                        app_id,
                        affected_memories,
                    )
                    intent_repo.renew_active_attempt(intent_id, attempt_token)
                    self.session.commit()
                if pending_error is not None:
                    raise pending_error

            upstream_completed = True
            ProjectRepository(self.session).lock_for_mutation(project_id)
            intent_repo = MutationIntentRepository(self.session)
            intent_repo.require_active_attempt(intent_id, attempt_token)
            event_repo = EventRepository(self.session)
            failed_count = len(failed)
            if failed_count == 0:
                status = "SUCCEEDED"
                event_repo.mark_succeeded(
                    event_id,
                    response={
                        "status": status,
                        "total": requested_count,
                        "count": deleted_count,
                        "success": True,
                    },
                )
            else:
                status = "PARTIAL" if deleted_count else "FAILED"
                event_repo.mark_failed(
                    event_id,
                    error={
                        "status": status,
                        "requested_count": requested_count,
                        "deleted_count": deleted_count,
                        "failed_count": failed_count,
                        "failed": failed,
                    },
                )
            result = {
                "status": status,
                "requested_count": requested_count,
                "deleted_count": deleted_count,
                "failed_count": failed_count,
                "failed": failed,
                "event_id": event_id,
            }
            if failed_count:
                intent_repo.fail(
                    intent_id,
                    status=status,
                    error=result,
                    result=result,
                )
            else:
                intent_repo.complete(intent_id, result=result)
            self.session.commit()
            return result
        except BaseException as exc:
            recovery_service._record_intent_failure(
                project_id,
                intent_id,
                attempt_token,
                exc,
                outcome_unknown=(
                    not isinstance(exc, Exception)
                    or upstream_completed
                    or upstream_effect_observed
                    or (
                        upstream_attempted
                        and _safe_outcome_unknown(exc)
                    )
                ),
                mark_event_failed=isinstance(exc, Exception),
            )
            if isinstance(exc, MutationIntentFenceError):
                raise MutationConflictError(
                    "Durable entity deletion attempt was superseded"
                ) from exc
            raise
