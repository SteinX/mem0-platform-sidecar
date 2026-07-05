import json
from typing import Any

import pytest
from sqlalchemy.orm import Session

from mem0_sidecar.core.memory_ops import (
    SIDECAR_APP_ID_METADATA_KEY,
    SIDECAR_PROJECT_ID_METADATA_KEY,
    MemoryService,
    extract_memory_id,
    extract_memory_ids,
)
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


class FailingAddMem0Client(FakeMem0Client):
    async def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.add_payloads.append(payload)
        raise RuntimeError("boom")


class FailingDeleteMem0Client(FakeMem0Client):
    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        self.deleted_ids.append(memory_id)
        raise RuntimeError("boom")


class MissingGetMem0Client(FakeMem0Client):
    async def get_memory(self, memory_id: str) -> dict[str, Any]:
        self.get_memory_ids.append(memory_id)
        return {"results": None}


def test_extract_memory_id_accepts_common_shapes() -> None:
    assert extract_memory_id({"id": "mem-1"}) == "mem-1"
    assert extract_memory_id({"memory_id": "mem-2"}) == "mem-2"
    assert extract_memory_id({"results": [{"id": "mem-3"}]}) == "mem-3"


def test_extract_memory_ids_collects_top_level_and_results_ids() -> None:
    assert extract_memory_ids(
        {
            "id": "mem-1",
            "memory_id": "mem-2",
            "results": [
                {"id": "mem-3"},
                {"memory_id": "mem-4"},
                {"id": "mem-3"},
                {"memory": "missing-id"},
            ],
        }
    ) == ["mem-1", "mem-2", "mem-3", "mem-4"]


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
    assert "app_id" not in mem0.add_payloads[0]
    assert mem0.add_payloads[0]["metadata"] == {
        "type": "decision",
        SIDECAR_PROJECT_ID_METADATA_KEY: "repo-a",
        SIDECAR_APP_ID_METADATA_KEY: "repo-a",
    }
    event = EventRepository(db_session).get(result["event"]["id"])
    assert json.loads(event.request_json)["metadata"] == {
        "type": "decision",
        SIDECAR_PROJECT_ID_METADATA_KEY: "repo-a",
        SIDECAR_APP_ID_METADATA_KEY: "repo-a",
    }


@pytest.mark.asyncio
async def test_memory_service_add_ignores_disabled_categories(db_session) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    CategoryRepository(db_session).replace_project_categories(
        project_id="repo-a",
        categories=[
            {
                "name": "decision",
                "description": "Architecture decisions",
                "enabled": False,
            }
        ],
    )
    service = MemoryService(session=db_session, mem0=FakeMem0Client())

    await service.add_memory(
        project_id="repo-a",
        payload={
            "text": "Use a sidecar control plane",
            "user_id": "root",
            "metadata": {"type": "decision"},
        },
    )
    db_session.commit()

    indexed = db_session.query(MemoryIndex).filter_by(
        project_id="repo-a",
        mem0_memory_id="mem-1",
    ).one()
    assert indexed.category is None


class ResultsOnlyAddMem0Client(FakeMem0Client):
    async def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.add_payloads.append(payload)
        return {
            "results": [
                {"id": "mem-1", "memory": payload["text"]},
                {"memory_id": "mem-2", "memory": payload["text"]},
            ]
        }


@pytest.mark.asyncio
async def test_memory_service_add_indexes_all_ids_from_results_response(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    service = MemoryService(session=db_session, mem0=ResultsOnlyAddMem0Client())

    result = await service.add_memory(
        project_id="repo-a",
        payload={"text": "hello", "user_id": "root", "app_id": "app-a"},
    )
    db_session.commit()

    indexed_ids = {
        memory.mem0_memory_id
        for memory in db_session.query(MemoryIndex).filter_by(project_id="repo-a").all()
    }

    assert indexed_ids == {"mem-1", "mem-2"}
    assert result["event"]["subject_id"] == "mem-1"


@pytest.mark.asyncio
async def test_memory_service_search_memories_preserves_normalized_scope(
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
        user_id="root",
        agent_id=None,
        app_id="repo-a",
        run_id=None,
        category=None,
        metadata={},
    )
    mem0 = FakeMem0Client()
    service = MemoryService(session=db_session, mem0=mem0)

    result = await service.search_memories(
        project_id="repo-a",
        payload={"text": "hello", "user_id": "root"},
    )

    assert result["results"][0]["id"] == "mem-1"
    assert mem0.search_payloads[0]["user_id"] == "root"
    assert "app_id" not in mem0.search_payloads[0]
    assert mem0.search_payloads[0]["filters"] == {
        SIDECAR_PROJECT_ID_METADATA_KEY: "repo-a",
        SIDECAR_APP_ID_METADATA_KEY: "repo-a",
    }


class ScopedSearchMem0Client(FakeMem0Client):
    async def search_memories(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.search_payloads.append(payload)
        return {
            "results": [
                {"id": "mem-app-a", "memory": "hello app a"},
                {"memory_id": "mem-app-b", "memory": "hello app b"},
                {"id": "mem-repo-b", "memory": "hello repo b"},
                {"memory": "missing-id"},
            ]
        }


@pytest.mark.asyncio
async def test_memory_service_search_filters_upstream_results_by_indexed_scope(
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
        project_id="repo-a",
        mem0_memory_id="mem-app-a",
        user_id="root",
        agent_id=None,
        app_id="app-a",
        run_id=None,
        category=None,
        metadata={},
    )
    MemoryIndexRepository(db_session).upsert_memory(
        project_id="repo-a",
        mem0_memory_id="mem-app-b",
        user_id="root",
        agent_id=None,
        app_id="app-b",
        run_id=None,
        category=None,
        metadata={},
    )
    MemoryIndexRepository(db_session).upsert_memory(
        project_id="repo-b",
        mem0_memory_id="mem-repo-b",
        user_id="root",
        agent_id=None,
        app_id="app-a",
        run_id=None,
        category=None,
        metadata={},
    )

    service = MemoryService(session=db_session, mem0=ScopedSearchMem0Client())

    result = await service.search_memories(
        project_id="repo-a",
        payload={
            "query": "hello",
            "user_id": "root",
            "app_id": "app-a",
            "filters": {"topic": "scope-test"},
        },
    )

    assert result["results"] == [{"id": "mem-app-a", "memory": "hello app a"}]
    assert service.mem0.search_payloads[0]["filters"] == {
        "topic": "scope-test",
        SIDECAR_PROJECT_ID_METADATA_KEY: "repo-a",
        SIDECAR_APP_ID_METADATA_KEY: "app-a",
    }


@pytest.mark.asyncio
async def test_memory_service_get_memory_uses_project_index(
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
async def test_memory_service_get_memory_rejects_wrong_app_without_remote_call(
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
        user_id="root",
        agent_id="codex",
        app_id="app-a",
        run_id=None,
        category=None,
        metadata={},
    )

    mem0 = FakeMem0Client()
    service = MemoryService(session=db_session, mem0=mem0)

    with pytest.raises((KeyError, ValueError)):
        await service.get_memory(
            project_id="repo-a",
            memory_id="mem-1",
            request_app_id="app-b",
        )

    assert mem0.get_memory_ids == []


@pytest.mark.asyncio
async def test_memory_service_get_memory_defaults_to_project_app_scope(
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
        user_id="root",
        agent_id="codex",
        app_id="app-a",
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
async def test_memory_service_get_memory_rejects_missing_upstream_memory(
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
        user_id="root",
        agent_id="codex",
        app_id="repo-a",
        run_id=None,
        category=None,
        metadata={},
    )

    mem0 = MissingGetMem0Client()
    service = MemoryService(session=db_session, mem0=mem0)

    with pytest.raises(KeyError):
        await service.get_memory(project_id="repo-a", memory_id="mem-1")

    assert mem0.get_memory_ids == ["mem-1"]


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
async def test_memory_service_delete_rejects_unknown_project_without_remote_delete(
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
async def test_memory_service_delete_rejects_wrong_app_without_remote_delete(
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
        app_id="app-a",
        category=None,
        metadata={},
    )

    mem0 = FakeMem0Client()
    service = MemoryService(session=db_session, mem0=mem0)

    with pytest.raises((KeyError, ValueError)):
        await service.delete_memory(
            project_id="repo-a",
            memory_id="mem-1",
            request_app_id="app-b",
        )

    assert mem0.deleted_ids == []
    failed_event = EventRepository(db_session).list_project_events("repo-a")[0]
    assert failed_event.status is EventStatus.FAILED
    assert json.loads(failed_event.request_json)["app_id"] == "app-b"


@pytest.mark.asyncio
async def test_memory_service_delete_defaults_to_project_app_scope(
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
        app_id="app-a",
        category=None,
        metadata={},
    )

    mem0 = FakeMem0Client()
    service = MemoryService(session=db_session, mem0=mem0)

    with pytest.raises((KeyError, ValueError)):
        await service.delete_memory(project_id="repo-a", memory_id="mem-1")

    assert mem0.deleted_ids == []
    failed_event = EventRepository(db_session).list_project_events("repo-a")[0]
    assert failed_event.status is EventStatus.FAILED
    assert json.loads(failed_event.request_json)["app_id"] == "repo-a"


@pytest.mark.asyncio
async def test_memory_service_delete_rejects_tombstoned_memory_without_remote_delete(
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


@pytest.mark.asyncio
async def test_memory_service_add_persists_failed_event_before_reraising(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )

    service = MemoryService(session=db_session, mem0=FailingAddMem0Client())

    with pytest.raises(RuntimeError, match="boom"):
        await service.add_memory(
            project_id="repo-a",
            payload={"text": "hello", "user_id": "root"},
        )

    db_session.rollback()

    with Session(db_session.get_bind()) as verification_session:
        event = verification_session.query(MemoryIndex).filter_by(
            project_id="repo-a",
            mem0_memory_id="mem-1",
        ).one_or_none()
        failed_event = EventRepository(
            verification_session
        ).list_project_events("repo-a")

    assert event is None
    assert len(failed_event) == 1
    assert failed_event[0].status is EventStatus.FAILED


@pytest.mark.asyncio
async def test_memory_service_delete_persists_failed_event_before_reraising(
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
        agent_id="codex",
        app_id="repo-a",
        run_id=None,
        category=None,
        metadata={},
    )

    service = MemoryService(session=db_session, mem0=FailingDeleteMem0Client())

    with pytest.raises(RuntimeError, match="boom"):
        await service.delete_memory(project_id="repo-a", memory_id="mem-1")

    db_session.rollback()

    with Session(db_session.get_bind()) as verification_session:
        memory = MemoryIndexRepository(verification_session).get_memory(
            project_id="repo-a",
            mem0_memory_id="mem-1",
        )
        failed_event = EventRepository(
            verification_session
        ).list_project_events("repo-a")

    assert memory is not None
    assert len(failed_event) == 1
    assert failed_event[0].status is EventStatus.FAILED
