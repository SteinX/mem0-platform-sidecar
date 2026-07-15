import asyncio
import threading

import pytest
from sqlalchemy import select

from mem0_sidecar.core.entities import EntityService
from mem0_sidecar.core.memory_ops import (
    SIDECAR_APP_ID_METADATA_KEY,
    SIDECAR_MUTATION_ID_METADATA_KEY,
    SIDECAR_PROJECT_ID_METADATA_KEY,
    MemoryService,
)
from mem0_sidecar.store.database import create_engine_from_url, create_session_factory
from mem0_sidecar.store.models import Base, Entity, MemoryIndex, MutationIntent
from mem0_sidecar.store.repositories import (
    EntityRepository,
    EventRepository,
    MemoryIndexRepository,
    MutationIntentRepository,
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


class _RecoveryGapClient:
    def __init__(self, upstream_called: threading.Event, order: list[str]) -> None:
        self.upstream_called = upstream_called
        self.order = order
        self.records: dict[str, dict] = {}
        self.add_calls = 0
        self._lock = threading.Lock()

    async def add_memory(self, payload):
        with self._lock:
            self.add_calls += 1
            call_number = self.add_calls
        memory_id = f"recovery-gap-{call_number}"
        record = {
            "id": memory_id,
            "memory": payload["text"],
            "app_id": APP_ID,
            "metadata": dict(payload.get("metadata") or {}),
        }
        self.records[memory_id] = record
        if call_number == 1:
            raise asyncio.CancelledError()
        self.order.append("a_upstream")
        self.upstream_called.set()
        return dict(record)

    async def list_memories(self, params):
        assert params["show_expired"] is True
        assert any(
            SIDECAR_MUTATION_ID_METADATA_KEY in record["metadata"]
            for record in self.records.values()
        )
        return {"results": list(self.records.values()), "total": len(self.records)}


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


def test_sqlite_recovery_branch_holds_project_lock_through_caller_intent_commit(
    tmp_path,
    monkeypatch,
) -> None:
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'recovery-gap.sqlite3'}")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        ProjectRepository(session).upsert_default_project(
            project_id=PROJECT_ID,
            name=PROJECT_ID,
            mem0_base_url="http://mem0.invalid",
            default_app_id=APP_ID,
        )
        session.commit()

    final_check_entered = threading.Event()
    release_final_check = threading.Event()
    b_lock_attempted = threading.Event()
    b_intent_committed = threading.Event()
    abort_b = threading.Event()
    a_upstream_called = threading.Event()
    order: list[str] = []
    failures: list[BaseException] = []
    client = _RecoveryGapClient(a_upstream_called, order)

    with session_factory() as session:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                MemoryService(session=session, mem0=client).add_memory(
                    project_id=PROJECT_ID,
                    payload={"text": "ambiguous-old", "app_id": APP_ID},
                )
            )
    with session_factory() as session:
        assert session.scalar(select(MutationIntent.status)) == "UNKNOWN"

    real_list_blocking = MutationIntentRepository.list_blocking

    def pause_after_final_blocker_read(self, project_id, app_id):
        blockers = real_list_blocking(self, project_id, app_id)
        if threading.current_thread().name == "recovery-request-a":
            final_check_entered.set()
            assert release_final_check.wait(5)
        return blockers

    monkeypatch.setattr(
        MutationIntentRepository,
        "list_blocking",
        pause_after_final_blocker_read,
    )

    def request_a() -> None:
        try:
            with session_factory() as session:
                asyncio.run(
                    MemoryService(session=session, mem0=client).add_memory(
                        project_id=PROJECT_ID,
                        payload={"text": "request-a", "app_id": APP_ID},
                    )
                )
        except BaseException as exc:
            failures.append(exc)

    def competing_request_b() -> None:
        try:
            with session_factory() as session:
                b_lock_attempted.set()
                ProjectRepository(session).lock_for_mutation(PROJECT_ID)
                if abort_b.is_set():
                    session.rollback()
                    return
                event = EventRepository(session).create_event(
                    project_id=PROJECT_ID,
                    app_id=APP_ID,
                    operation="memory.add",
                    request={"app_id": APP_ID, "text": "request-b"},
                    subject_type="memory",
                )
                MutationIntentRepository(session).create(
                    project_id=PROJECT_ID,
                    app_id=APP_ID,
                    event_id=event.id,
                    operation="memory.add",
                    operation_key="stranded-request-b",
                    payload={"mutation_id": "stranded-request-b"},
                )
                session.commit()
                order.append("b_intent_committed")
                b_intent_committed.set()
        except BaseException as exc:
            failures.append(exc)

    a_thread = threading.Thread(
        target=request_a,
        name="recovery-request-a",
        daemon=True,
    )
    b_thread = threading.Thread(
        target=competing_request_b,
        name="competing-request-b",
        daemon=True,
    )
    a_thread.start()
    assert final_check_entered.wait(3)
    b_thread.start()
    assert b_lock_attempted.wait(3)
    committed_in_gap = b_intent_committed.wait(0.35)
    abort_b.set()
    release_final_check.set()
    a_thread.join(8)
    b_thread.join(8)

    assert not a_thread.is_alive()
    assert not b_thread.is_alive()
    assert failures == []
    assert not committed_in_gap, (
        "competing intent committed after recovery's blocker read but before "
        "request A's caller intent commit"
    )
    assert a_upstream_called.is_set()
    assert order == ["a_upstream"]
    with session_factory() as session:
        assert list(
            session.scalars(
                select(MutationIntent.status).where(
                    MutationIntent.status == "ACTIVE"
                )
            )
        ) == []
