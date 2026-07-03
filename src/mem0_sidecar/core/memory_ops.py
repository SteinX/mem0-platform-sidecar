from typing import Any

from sqlalchemy.orm import Session

from mem0_sidecar.core.categories import extract_category
from mem0_sidecar.core.scope import normalize_scope
from mem0_sidecar.store.models import MemoryIndex
from mem0_sidecar.store.repositories import (
    CategoryRepository,
    EntityRepository,
    EventRepository,
    MemoryIndexRepository,
)


def extract_memory_id(response: dict[str, Any]) -> str:
    if isinstance(response.get("id"), str):
        return response["id"]
    if isinstance(response.get("memory_id"), str):
        return response["memory_id"]
    results = response.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        first_id = results[0].get("id") or results[0].get("memory_id")
        if isinstance(first_id, str):
            return first_id
    raise ValueError(f"Could not extract memory id from response: {response!r}")


def _event_payload(event: Any) -> dict[str, Any]:
    return {
        "id": event.id,
        "operation": event.operation,
        "status": event.status,
        "subject_type": event.subject_type,
        "subject_id": event.subject_id,
    }


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
        oss_payload = dict(payload)
        oss_payload.update(scope.as_filter_dict())

        event_repo = EventRepository(self.session)
        event = event_repo.create_event(
            project_id=project_id,
            operation="memory.add",
            request=oss_payload,
            subject_type="memory",
        )
        try:
            memory_response = await self.mem0.add_memory(oss_payload)
            memory_id = extract_memory_id(memory_response)
            MemoryIndexRepository(self.session).upsert_memory(
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
            event.subject_id = memory_id
            event_repo.mark_succeeded(event.id, response=memory_response)
            return {"memory": memory_response, "event": _event_payload(event)}
        except Exception as exc:
            event_repo.mark_failed(event.id, error={"message": str(exc)})
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
        oss_payload = dict(payload)
        oss_payload.update(scope.as_filter_dict())
        return await self.mem0.search_memories(oss_payload)

    async def get_memory(self, *, memory_id: str) -> dict[str, Any]:
        return await self.mem0.get_memory(memory_id)

    async def delete_memory(
        self,
        *,
        project_id: str,
        memory_id: str,
    ) -> dict[str, Any]:
        event_repo = EventRepository(self.session)
        event = event_repo.create_event(
            project_id=project_id,
            operation="memory.delete",
            request={"memory_id": memory_id},
            subject_type="memory",
            subject_id=memory_id,
        )
        try:
            response = await self.mem0.delete_memory(memory_id)
            MemoryIndexRepository(self.session).delete_memory(
                project_id=project_id,
                mem0_memory_id=memory_id,
            )
            event_repo.mark_succeeded(event.id, response=response)
            return {"memory": response, "event": _event_payload(event)}
        except Exception as exc:
            event_repo.mark_failed(event.id, error={"message": str(exc)})
            raise
