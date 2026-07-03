import json
from typing import Any

import pytest

from mem0_sidecar.core.memory_ops import MemoryService, extract_memory_id
from mem0_sidecar.store.models import EventStatus, MemoryIndex
from mem0_sidecar.store.repositories import (
    CategoryRepository,
    EventRepository,
    MemoryIndexRepository,
    ProjectRepository,
)


class FakeMem0Client:
    def __init__(self) -> None:
        self.add_payloads: list[dict[str, Any]] = []
        self.search_payloads: list[dict[str, Any]] = []
        self.get_memory_ids: list[str] = []
        self.deleted_ids: list[str] = []

    async def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.add_payloads.append(payload)
        return {"id": "mem-1", "memory": payload["text"]}

    async def search_memories(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.search_payloads.append(payload)
        return {"results": [{"id": "mem-1", "memory": "hello"}]}

    async def get_memory(self, memory_id: str) -> dict[str, Any]:
        self.get_memory_ids.append(memory_id)
        return {"id": memory_id, "memory": "hello"}

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        self.deleted_ids.append(memory_id)
        return {"message": "Deleted"}


def test_extract_memory_id_accepts_common_shapes() -> None:
    assert extract_memory_id({"id": "mem-1"}) == "mem-1"
    assert extract_memory_id({"memory_id": "mem-2"}) == "mem-2"
    assert extract_memory_id({"results": [{"id": "mem-3"}]}) == "mem-3"


@pytest.mark.asyncio
async def test_memory_service_adds_memory_indexes_projection_and_event(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
        default_user_id="root",
        default_agent_id="codex",
    )
    CategoryRepository(db_session).replace_project_categories(
        project_id="repo-a",
        categories=[{"name": "decision", "description": "Architecture decisions"}],
    )
    mem0 = FakeMem0Client()
    service = MemoryService(session=db_session, mem0=mem0)

    result = await service.add_memory(
        project_id="repo-a",
        payload={
            "text": "Use a sidecar control plane",
            "user_id": "root",
            "agent_id": "codex",
            "metadata": {"type": "decision"},
        },
    )
    db_session.commit()

    indexed = db_session.query(MemoryIndex).filter_by(
        project_id="repo-a",
        mem0_memory_id="mem-1",
    ).one()
    assert result["memory"]["id"] == "mem-1"
    assert result["event"]["status"] == EventStatus.SUCCEEDED
    assert indexed.app_id == "repo-a"
    assert indexed.category == "decision"
    assert mem0.add_payloads[0]["user_id"] == "root"
    assert mem0.add_payloads[0]["app_id"] == "repo-a"
    event = EventRepository(db_session).get(result["event"]["id"])
    assert json.loads(event.request_json)["app_id"] == "repo-a"


@pytest.mark.asyncio
async def test_memory_service_search_memories_preserves_normalized_scope(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    mem0 = FakeMem0Client()
    service = MemoryService(session=db_session, mem0=mem0)

    result = await service.search_memories(
        project_id="repo-a",
        payload={"text": "hello", "user_id": "root"},
    )

    assert result["results"][0]["id"] == "mem-1"
    assert mem0.search_payloads[0]["user_id"] == "root"
    assert mem0.search_payloads[0]["app_id"] == "repo-a"


@pytest.mark.asyncio
async def test_memory_service_get_memory_scopes_by_project_projection(db_session) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    MemoryIndexRepository(db_session).upsert_memory(
        project_id="repo-a",
        mem0_memory_id="mem-1",
        user_id="root",
        agent_id="codex",
        app_id="repo-a",
        run_id=None,
        category=None,
        metadata={},
    )

    mem0 = FakeMem0Client()
    service = MemoryService(session=db_session, mem0=mem0)

    result = await service.get_memory(project_id="repo-a", memory_id="mem-1")

    assert result == {"id": "mem-1", "memory": "hello"}
    assert mem0.get_memory_ids == ["mem-1"]


@pytest.mark.asyncio
async def test_memory_service_get_memory_rejects_wrong_project_without_remote_call(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-b",
        name="Repo B",
        mem0_base_url="http://mem0:8000",
    )
    MemoryIndexRepository(db_session).upsert_memory(
        project_id="repo-b",
        mem0_memory_id="mem-1",
        user_id="root",
        agent_id="codex",
        app_id="repo-b",
        run_id=None,
        category=None,
        metadata={},
    )

    mem0 = FakeMem0Client()
    service = MemoryService(session=db_session, mem0=mem0)

    with pytest.raises((KeyError, ValueError)):
        await service.get_memory(project_id="repo-a", memory_id="mem-1")

    assert mem0.get_memory_ids == []


@pytest.mark.asyncio
async def test_memory_service_delete_uses_projection_scope_for_event_request(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
        default_user_id="root",
        default_agent_id="codex",
    )
    MemoryIndexRepository(db_session).upsert_memory(
        project_id="repo-a",
        mem0_memory_id="mem-1",
        user_id="root",
        agent_id="codex",
        app_id="repo-a",
        run_id="session-1",
        category=None,
        metadata={},
    )

    service = MemoryService(session=db_session, mem0=FakeMem0Client())
    result = await service.delete_memory(project_id="repo-a", memory_id="mem-1")

    assert result["memory"]["message"] == "Deleted"
    event = EventRepository(db_session).get(result["event"]["id"])
    assert json.loads(event.request_json) == {
        "memory_id": "mem-1",
        "user_id": "root",
        "agent_id": "codex",
        "app_id": "repo-a",
        "run_id": "session-1",
    }


@pytest.mark.asyncio
async def test_memory_service_delete_rejects_unknown_project_projection_without_remote_delete(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-b",
        name="Repo B",
        mem0_base_url="http://mem0:8000",
    )
    MemoryIndexRepository(db_session).upsert_memory(
        project_id="repo-b",
        mem0_memory_id="mem-1",
        user_id="alice",
        app_id="repo-b",
        category=None,
        metadata={},
    )

    mem0 = FakeMem0Client()
    service = MemoryService(session=db_session, mem0=mem0)

    with pytest.raises((KeyError, ValueError)):
        await service.delete_memory(project_id="repo-a", memory_id="mem-1")

    assert mem0.deleted_ids == []


@pytest.mark.asyncio
async def test_memory_service_delete_rejects_tombstoned_projection_without_remote_delete(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    MemoryIndexRepository(db_session).upsert_memory(
        project_id="repo-a",
        mem0_memory_id="mem-1",
        user_id="alice",
        app_id="repo-a",
        category=None,
        metadata={},
    )
    MemoryIndexRepository(db_session).delete_memory(
        project_id="repo-a",
        mem0_memory_id="mem-1",
    )

    mem0 = FakeMem0Client()
    service = MemoryService(session=db_session, mem0=mem0)

    with pytest.raises((KeyError, ValueError)):
        await service.delete_memory(project_id="repo-a", memory_id="mem-1")

    assert mem0.deleted_ids == []
