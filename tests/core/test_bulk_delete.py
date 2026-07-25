import asyncio
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from mem0_sidecar.core.memory_ops import MemoryService, MutationConflictError
from mem0_sidecar.mem0_client.client import Mem0UpstreamError
from mem0_sidecar.store.database import create_engine_from_url, create_session_factory
from mem0_sidecar.store.models import (
    Base,
    Event,
    MutationIntent,
    MutationIntentTarget,
)
from mem0_sidecar.store.repositories import (
    EventRepository,
    MemoryIndexRepository,
    MutationIntentFenceError,
    MutationIntentRepository,
    ProjectRepository,
)


class RetryableBulkDeleteMem0Client:
    def __init__(self) -> None:
        self.records = {
            "mem-1": {"id": "mem-1", "memory": "one"},
            "mem-2": {"id": "mem-2", "memory": "two"},
        }
        self.delete_calls: list[str] = []
        self.failed_once = False

    async def get_memory(self, memory_id: str) -> dict[str, Any]:
        record = self.records.get(memory_id)
        if record is None:
            raise Mem0UpstreamError(
                method="GET",
                path=f"/memories/{memory_id}",
                status_code=404,
                response_text="not found",
                outcome_unknown=False,
                message="not found",
            )
        return record

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        self.delete_calls.append(memory_id)
        if memory_id == "mem-2" and not self.failed_once:
            self.failed_once = True
            raise Mem0UpstreamError(
                method="DELETE",
                path=f"/memories/{memory_id}",
                status_code=500,
                response_text="retry",
                outcome_unknown=False,
                message="retry",
            )
        self.records.pop(memory_id, None)
        return {"message": "Memory deleted successfully"}


class BlockingBulkDeleteMem0Client(RetryableBulkDeleteMem0Client):
    def __init__(self) -> None:
        super().__init__()
        self.failed_once = True
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        self.delete_calls.append(memory_id)
        if not self.started.is_set():
            self.started.set()
            await self.release.wait()
        self.records.pop(memory_id, None)
        return {"message": "Memory deleted successfully"}


def _seed_bulk_scope(session: Session) -> None:
    ProjectRepository(session).upsert_default_project(
        project_id="repo-a",
        name="repo-a",
        mem0_base_url="http://mem0.local",
        default_app_id="app-a",
    )
    memories = MemoryIndexRepository(session)
    for memory_id in ("mem-1", "mem-2"):
        memories.upsert_memory(
            project_id="repo-a",
            mem0_memory_id=memory_id,
            user_id="u1",
            app_id="app-a",
            category=None,
            metadata={},
        )
    session.commit()


@pytest.mark.asyncio
async def test_bulk_delete_retry_skips_completed_targets(
    db_session: Session,
) -> None:
    _seed_bulk_scope(db_session)
    mem0 = RetryableBulkDeleteMem0Client()
    service = MemoryService(session=db_session, mem0=mem0)

    with pytest.raises(MutationConflictError, match="retry"):
        await service.bulk_delete_memories(
            project_id="repo-a",
            app_id="app-a",
            filters={"user_id": "u1"},
        )

    result = await service.bulk_delete_memories(
        project_id="repo-a",
        app_id="app-a",
        filters={"user_id": "u1"},
    )

    assert result == {"message": "All relevant memories deleted"}
    assert mem0.delete_calls.count("mem-1") == 1
    assert mem0.delete_calls.count("mem-2") == 2
    assert MemoryIndexRepository(db_session).list_scoped_memory_ids(
        project_id="repo-a",
        mem0_memory_ids=["mem-1", "mem-2"],
        user_id="u1",
        app_id="app-a",
        agent_id=None,
        run_id=None,
    ) == set()

    intents = list(
        db_session.scalars(
            select(MutationIntent).where(
                MutationIntent.operation == "memory.bulk_delete"
            )
        )
    )
    assert len(intents) == 1
    assert intents[0].status == "COMPLETED"
    assert {
        target.status
        for target in db_session.scalars(
            select(MutationIntentTarget).where(
                MutationIntentTarget.intent_id == intents[0].id
            )
        )
    } == {"COMPLETED"}

    events = list(
        db_session.scalars(
            select(Event)
            .where(Event.operation == "memory.bulk_delete")
            .order_by(Event.created_at)
        )
    )
    assert [event.status for event in events] == ["FAILED", "SUCCEEDED"]


@pytest.mark.asyncio
async def test_completed_bulk_delete_does_not_suppress_later_matching_memories(
    db_session: Session,
) -> None:
    _seed_bulk_scope(db_session)
    mem0 = RetryableBulkDeleteMem0Client()
    mem0.failed_once = True
    service = MemoryService(session=db_session, mem0=mem0)

    first = await service.bulk_delete_memories(
        project_id="repo-a",
        app_id="app-a",
        filters={"user_id": "u1"},
    )
    MemoryIndexRepository(db_session).upsert_memory(
        project_id="repo-a",
        mem0_memory_id="mem-later",
        user_id="u1",
        app_id="app-a",
        category=None,
        metadata={},
    )
    db_session.commit()
    mem0.records["mem-later"] = {"id": "mem-later", "memory": "later"}

    second = await service.bulk_delete_memories(
        project_id="repo-a",
        app_id="app-a",
        filters={"user_id": "u1"},
    )

    assert first == second == {"message": "All relevant memories deleted"}
    assert mem0.delete_calls.count("mem-later") == 1
    assert (
        len(
            list(
                db_session.scalars(
                    select(MutationIntent).where(
                        MutationIntent.operation == "memory.bulk_delete"
                    )
                )
            )
        )
        == 2
    )


@pytest.mark.asyncio
async def test_bulk_delete_crash_after_upstream_delete_is_recoverable(
    db_session: Session,
    monkeypatch,
) -> None:
    _seed_bulk_scope(db_session)
    mem0 = RetryableBulkDeleteMem0Client()
    mem0.failed_once = True
    service = MemoryService(session=db_session, mem0=mem0)
    original_delete = MemoryIndexRepository.delete_memory
    crashed = False

    def crash_once(repository, *, project_id, mem0_memory_id):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("simulated projection crash")
        return original_delete(
            repository,
            project_id=project_id,
            mem0_memory_id=mem0_memory_id,
        )

    monkeypatch.setattr(MemoryIndexRepository, "delete_memory", crash_once)

    with pytest.raises(RuntimeError, match="simulated projection crash"):
        await service.bulk_delete_memories(
            project_id="repo-a",
            app_id="app-a",
            filters={"user_id": "u1"},
        )

    intent = db_session.scalar(
        select(MutationIntent).where(
            MutationIntent.operation == "memory.bulk_delete"
        )
    )
    event = db_session.scalar(
        select(Event).where(Event.operation == "memory.bulk_delete")
    )
    assert intent is not None and intent.status == "UNKNOWN"
    assert intent.lease_expires_at is None
    assert event is not None and event.status == "FAILED"
    db_session.rollback()

    result = await service.bulk_delete_memories(
        project_id="repo-a",
        app_id="app-a",
        filters={"user_id": "u1"},
    )

    assert result == {"message": "All relevant memories deleted"}
    assert db_session.get(MutationIntent, intent.id).status == "COMPLETED"


@pytest.mark.asyncio
async def test_concurrent_bulk_delete_requests_share_one_intent(tmp_path) -> None:
    engine = create_engine_from_url(
        f"sqlite:///{tmp_path / 'bulk-delete-concurrency.sqlite3'}"
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        _seed_bulk_scope(session)
    mem0 = BlockingBulkDeleteMem0Client()

    async def invoke() -> dict[str, str]:
        with session_factory() as session:
            return await MemoryService(
                session=session,
                mem0=mem0,
            ).bulk_delete_memories(
                project_id="repo-a",
                app_id="app-a",
                filters={"user_id": "u1"},
            )

    first = asyncio.create_task(invoke())
    await asyncio.wait_for(mem0.started.wait(), timeout=2)
    with session_factory() as session:
        MemoryIndexRepository(session).upsert_memory(
            project_id="repo-a",
            mem0_memory_id="mem-later",
            user_id="u1",
            app_id="app-a",
            category=None,
            metadata={},
        )
        session.commit()
    mem0.records["mem-later"] = {"id": "mem-later", "memory": "later"}
    with pytest.raises(MutationConflictError, match="already in progress"):
        await invoke()
    mem0.release.set()

    assert await asyncio.wait_for(first, timeout=2) == {
        "message": "All relevant memories deleted"
    }
    with session_factory() as session:
        intents = list(
            session.scalars(
                select(MutationIntent).where(
                    MutationIntent.operation == "memory.bulk_delete"
                )
            )
        )
        assert len(intents) == 1
        assert intents[0].status == "COMPLETED"
        assert (
            MemoryIndexRepository(session).get_memory(
                project_id="repo-a",
                mem0_memory_id="mem-later",
                app_id="app-a",
            )
            is not None
        )


def test_bulk_delete_recovery_claim_uses_attempt_fence(tmp_path) -> None:
    engine = create_engine_from_url(
        f"sqlite:///{tmp_path / 'bulk-delete-claim.sqlite3'}"
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        _seed_bulk_scope(session)
        event = EventRepository(session).create_event(
            project_id="repo-a",
            app_id="app-a",
            operation="memory.bulk_delete",
            request={"app_id": "app-a", "user_id": "u1"},
        )
        intent = MutationIntentRepository(session).create(
            project_id="repo-a",
            app_id="app-a",
            event_id=event.id,
            operation="memory.bulk_delete",
            payload={"request_fingerprint": "same"},
        )
        MutationIntentRepository(session).mark_unresolved(
            intent.id,
            error={"message": "retry"},
        )
        intent_id = intent.id
        session.commit()

    with session_factory() as first, session_factory() as second:
        first_intent = MutationIntentRepository(first).get(intent_id)
        second_intent = MutationIntentRepository(second).get(intent_id)
        first.commit()
        second.commit()

        assert MutationIntentRepository(first).claim_recovery(first_intent) is True
        first.commit()
        with pytest.raises(MutationIntentFenceError):
            MutationIntentRepository(second).claim_recovery(second_intent)


@pytest.mark.asyncio
async def test_bulk_delete_projection_change_after_upstream_delete_is_traced(
    tmp_path,
) -> None:
    engine = create_engine_from_url(
        f"sqlite:///{tmp_path / 'bulk-delete-projection-race.sqlite3'}"
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        _seed_bulk_scope(session)

    class ProjectionChangingClient(RetryableBulkDeleteMem0Client):
        async def delete_memory(self, memory_id: str) -> dict[str, Any]:
            self.delete_calls.append(memory_id)
            self.records.pop(memory_id, None)
            with session_factory() as concurrent_session:
                memory = MemoryIndexRepository(concurrent_session).get_memory(
                    project_id="repo-a",
                    mem0_memory_id=memory_id,
                    app_id="app-a",
                )
                assert memory is not None
                MemoryIndexRepository(concurrent_session).upsert_memory(
                    project_id="repo-a",
                    mem0_memory_id=memory_id,
                    user_id=memory.user_id,
                    app_id="app-a",
                    category=memory.category,
                    metadata={"concurrent": True},
                )
                concurrent_session.commit()
            return {"message": "Memory deleted successfully"}

    with session_factory() as session:
        with pytest.raises(
            MutationConflictError,
            match="projection changed",
        ):
            await MemoryService(
                session=session,
                mem0=ProjectionChangingClient(),
            ).bulk_delete_memories(
                project_id="repo-a",
                app_id="app-a",
                filters={"user_id": "u1"},
            )

    with session_factory() as session:
        intent = session.scalar(
            select(MutationIntent).where(
                MutationIntent.operation == "memory.bulk_delete"
            )
        )
        event = session.scalar(
            select(Event).where(Event.operation == "memory.bulk_delete")
        )
        assert intent is not None and intent.status == "UNKNOWN"
        assert intent.lease_expires_at is None
        assert event is not None and event.status == "FAILED"
