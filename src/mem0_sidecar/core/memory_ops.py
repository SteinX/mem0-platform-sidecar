from typing import Any

from sqlalchemy.orm import Session

from mem0_sidecar.core.categories import extract_category
from mem0_sidecar.core.scope import Scope, normalize_scope
from mem0_sidecar.store.models import MemoryIndex
from mem0_sidecar.store.repositories import (
    CategoryRepository,
    EntityRepository,
    EventRepository,
    MemoryIndexRepository,
)

SIDECAR_PROJECT_ID_METADATA_KEY = "_mem0_sidecar_project_id"
SIDECAR_APP_ID_METADATA_KEY = "_mem0_sidecar_app_id"

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
    raise ValueError(f"Could not extract memory id from response: {response!r}")


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
    return filtered_response


def _event_payload(event: Any) -> dict[str, Any]:
    return {
        "id": event.id,
        "operation": event.operation,
        "status": event.status,
        "subject_type": event.subject_type,
        "subject_id": event.subject_id,
    }


def _validate_get_response(memory_id: str, response: Any) -> dict[str, Any]:
    if not isinstance(response, dict) or memory_id not in extract_memory_ids(response):
        raise KeyError(memory_id)
    return response


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

    async def add_memory(
        self,
        *,
        project_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
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

        event_repo = EventRepository(self.session)
        event = event_repo.create_event(
            project_id=project_id,
            operation="memory.add",
            request=oss_payload,
            subject_type="memory",
        )
        try:
            memory_response = await self.mem0.add_memory(oss_payload)
            memory_ids = extract_memory_ids(memory_response)
            if not memory_ids:
                raise ValueError(
                    f"Could not extract memory id from response: {memory_response!r}"
                )
            memory_repo = MemoryIndexRepository(self.session)
            for memory_id in memory_ids:
                memory_repo.upsert_memory(
                    project_id=project_id,
                    mem0_memory_id=memory_id,
                    user_id=scope.user_id,
                    app_id=scope.app_id,
                    agent_id=scope.agent_id,
                    run_id=scope.run_id,
                    category=category,
                    metadata=metadata,
                )
            EntityRepository(self.session).upsert_entity(
                project_id=project_id,
                entity_type="app",
                entity_id=scope.app_id,
                display_name=scope.app_id,
            )
            event.subject_id = memory_ids[0]
            event_repo.mark_succeeded(event.id, response=memory_response)
            return {"memory": memory_response, "event": _event_payload(event)}
        except Exception as exc:
            event_repo.mark_failed(event.id, error={"message": str(exc)})
            self.session.commit()
            raise

    async def search_memories(
        self,
        *,
        project_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
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
        response = await self.mem0.search_memories(oss_payload)
        return _filter_search_results(
            response,
            memory_repo=MemoryIndexRepository(self.session),
            scope=scope,
        )

    async def get_memory(
        self,
        *,
        project_id: str,
        memory_id: str,
        request_app_id: str | None = None,
    ) -> dict[str, Any]:
        memory_repo = MemoryIndexRepository(self.session)
        memory = memory_repo.get_memory(
            project_id=project_id,
            mem0_memory_id=memory_id,
            app_id=request_app_id,
        )
        if memory is None:
            raise KeyError(memory_id)

        return _validate_get_response(memory_id, await self.mem0.get_memory(memory_id))

    async def delete_memory(
        self,
        *,
        project_id: str,
        memory_id: str,
        request_app_id: str | None = None,
    ) -> dict[str, Any]:
        memory_repo = MemoryIndexRepository(self.session)
        memory = memory_repo.get_memory(
            project_id=project_id,
            mem0_memory_id=memory_id,
            app_id=request_app_id,
        )
        if memory is None:
            event_repo = EventRepository(self.session)
            event = event_repo.create_event(
                project_id=project_id,
                operation="memory.delete",
                request=_memory_delete_request(
                    project_id=project_id,
                    memory_id=memory_id,
                    memory=None,
                    request_app_id=request_app_id,
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
            operation="memory.delete",
            request=_memory_delete_request(
                project_id=project_id,
                memory_id=memory_id,
                memory=memory,
            ),
            subject_type="memory",
            subject_id=memory_id,
        )
        try:
            response = await self.mem0.delete_memory(memory_id)
            memory_repo.delete_memory(
                project_id=project_id,
                mem0_memory_id=memory_id,
            )
            event_repo.mark_succeeded(event.id, response=response)
            return {"memory": response, "event": _event_payload(event)}
        except Exception as exc:
            event_repo.mark_failed(event.id, error={"message": str(exc)})
            self.session.commit()
            raise
