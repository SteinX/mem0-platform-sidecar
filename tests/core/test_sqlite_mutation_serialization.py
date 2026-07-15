import asyncio
import threading

import pytest
from sqlalchemy import select

from mem0_sidecar.core.entities import EntityService
from mem0_sidecar.core.memory_ops import (
    SIDECAR_APP_ID_METADATA_KEY,
    SIDECAR_PROJECT_ID_METADATA_KEY,
    MemoryService,
)
from mem0_sidecar.store.database import create_engine_from_url, create_session_factory
from mem0_sidecar.store.models import Base, Entity, MemoryIndex
from mem0_sidecar.store.repositories import (
    EntityRepository,
    MemoryIndexRepository,
    ProjectRepository,
)

PROJECT_ID = "sqlite-lock-project"
APP_ID = "sqlite-lock-app"
MEMORY_ID = "sqlite-lock-memory"


class _BlockingMutationClient:
    def __init__(
        self,
        operation: str,
        started: threading.Event,
        release: threading.Event,
    ) -> None:
        self.operation = operation
        self.started = started
        self.release = release

    async def update_memory(self, memory_id, payload):
        assert self.operation == "update"
        self.started.set()
        assert self.release.wait(5)
        return {"id": memory_id, "updated": True}

    async def get_memory(self, memory_id):
        return {
            "id": memory_id,
            "memory": "updated",
            "user_id": "alice",
            "app_id": APP_ID,
            "metadata": {
                SIDECAR_PROJECT_ID_METADATA_KEY: PROJECT_ID,
                SIDECAR_APP_ID_METADATA_KEY: APP_ID,
            },
        }

    async def list_memories(self, params):
        assert self.operation == "reconcile"
        self.started.set()
        assert self.release.wait(5)
        return {"results": [await self.get_memory(MEMORY_ID)], "total": 1}


class _DeleteClient:
    def __init__(self, called: threading.Event) -> None:
        self.called = called

    async def delete_memory(self, memory_id):
        self.called.set()
        return {"id": memory_id, "deleted": True}


@pytest.mark.parametrize("operation", ["update", "reconcile"])
def test_sqlite_project_lock_prevents_entity_delete_resurrection(
    tmp_path,
    operation: str,
) -> None:
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'mutation-lock.sqlite3'}")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        ProjectRepository(session).upsert_default_project(
            project_id=PROJECT_ID,
            name=PROJECT_ID,
            mem0_base_url="http://mem0.invalid",
            default_app_id=APP_ID,
        )
        MemoryIndexRepository(session).upsert_memory(
            project_id=PROJECT_ID,
            mem0_memory_id=MEMORY_ID,
            user_id="alice",
            app_id=APP_ID,
            category=None,
            metadata={},
        )
        EntityRepository(session).rebuild_project_entities(PROJECT_ID, APP_ID)
        session.commit()

    started = threading.Event()
    release = threading.Event()
    delete_called = threading.Event()
    failures: list[BaseException] = []

    def mutate() -> None:
        try:
            with session_factory() as session:
                service = MemoryService(
                    session=session,
                    mem0=_BlockingMutationClient(operation, started, release),
                )
                if operation == "update":
                    asyncio.run(
                        service.update_memory(
                            project_id=PROJECT_ID,
                            memory_id=MEMORY_ID,
                            request_app_id=APP_ID,
                            payload={"text": "updated"},
                        )
                    )
                else:
                    asyncio.run(
                        service.reconcile_memories(
                            project_id=PROJECT_ID,
                            app_id=APP_ID,
                            adopt_unscoped=False,
                            allow_adopt_unscoped=False,
                            default_project_id=PROJECT_ID,
                        )
                    )
                session.commit()
        except BaseException as exc:
            failures.append(exc)

    def delete_entity() -> None:
        try:
            with session_factory() as session:
                asyncio.run(
                    EntityService(
                        session=session,
                        mem0=_DeleteClient(delete_called),
                    ).delete_entity(PROJECT_ID, APP_ID, "user", "alice")
                )
                session.commit()
        except BaseException as exc:
            failures.append(exc)

    mutation_thread = threading.Thread(target=mutate, daemon=True)
    delete_thread = threading.Thread(target=delete_entity, daemon=True)
    mutation_thread.start()
    assert started.wait(3)
    delete_thread.start()
    assert not delete_called.wait(0.25), (
        f"entity delete bypassed the SQLite {operation} project lock"
    )
    release.set()
    mutation_thread.join(8)
    delete_thread.join(8)

    assert not mutation_thread.is_alive()
    assert not delete_thread.is_alive()
    assert failures == []
    assert delete_called.is_set()
    with session_factory() as session:
        memory = session.scalar(
            select(MemoryIndex).where(
                MemoryIndex.project_id == PROJECT_ID,
                MemoryIndex.mem0_memory_id == MEMORY_ID,
            )
        )
        assert memory is not None and memory.deleted_at is not None
        assert session.scalar(
            select(Entity).where(
                Entity.project_id == PROJECT_ID,
                Entity.app_id == APP_ID,
                Entity.entity_type == "user",
                Entity.entity_id == "alice",
            )
        ) is None
