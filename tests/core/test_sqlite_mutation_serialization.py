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
    MutationConflictError,
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

    async def add_memory(self, payload):
        assert self.operation == "add"
        self.started.set()
        assert self.release.wait(5)
        return {"id": "sqlite-added-memory", "memory": payload["text"]}

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

    async def delete_memory(self, memory_id):
        assert self.operation in {"delete", "entity"}
        self.started.set()
        assert self.release.wait(5)
        return {"id": memory_id, "deleted": True}


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


class _BlockingRecoveryObservationClient:
    def __init__(self, records, started, release) -> None:
        self.records = records
        self.started = started
        self.release = release

    async def list_memories(self, params):
        self.started.set()
        assert self.release.wait(5)
        return {"results": list(self.records.values()), "total": len(self.records)}


@pytest.mark.parametrize(
    "operation",
    ["add", "update", "reconcile", "delete", "entity"],
)
def test_sqlite_unrelated_writer_progresses_during_upstream_mutation(
    tmp_path,
    operation: str,
) -> None:
    engine = create_engine_from_url(
        f"sqlite:///{tmp_path / 'mutation-await-release.sqlite3'}"
    )
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

    upstream_started = threading.Event()
    release_upstream = threading.Event()
    writer_done = threading.Event()
    failures: list[BaseException] = []

    def mutate() -> None:
        try:
            with session_factory() as session:
                service = MemoryService(
                    session=session,
                    mem0=_BlockingMutationClient(
                        operation,
                        upstream_started,
                        release_upstream,
                    ),
                )
                if operation == "add":
                    asyncio.run(
                        service.add_memory(
                            project_id=PROJECT_ID,
                            payload={"text": "added", "app_id": APP_ID},
                        )
                    )
                elif operation == "update":
                    asyncio.run(
                        service.update_memory(
                            project_id=PROJECT_ID,
                            memory_id=MEMORY_ID,
                            request_app_id=APP_ID,
                            payload={"text": "updated"},
                        )
                    )
                elif operation == "reconcile":
                    asyncio.run(
                        service.reconcile_memories(
                            project_id=PROJECT_ID,
                            app_id=APP_ID,
                            adopt_unscoped=False,
                            allow_adopt_unscoped=False,
                            default_project_id=PROJECT_ID,
                        )
                    )
                elif operation == "delete":
                    asyncio.run(
                        service.delete_memory(
                            project_id=PROJECT_ID,
                            memory_id=MEMORY_ID,
                            request_app_id=APP_ID,
                        )
                    )
                else:
                    asyncio.run(
                        EntityService(
                            session=session,
                            mem0=service.mem0,
                        ).delete_entity(
                            PROJECT_ID,
                            APP_ID,
                            "user",
                            "alice",
                        )
                    )
        except BaseException as exc:
            failures.append(exc)

    def unrelated_writer() -> None:
        try:
            with session_factory() as session:
                ProjectRepository(session).upsert_default_project(
                    project_id="unrelated-project",
                    name="unrelated-project",
                    mem0_base_url="http://mem0.invalid",
                    default_app_id="unrelated-app",
                )
                session.commit()
                writer_done.set()
        except BaseException as exc:
            failures.append(exc)

    mutation_thread = threading.Thread(target=mutate, daemon=True)
    writer_thread = threading.Thread(target=unrelated_writer, daemon=True)
    mutation_thread.start()
    assert upstream_started.wait(3)
    writer_thread.start()
    progressed_during_await = writer_done.wait(0.75)
    release_upstream.set()
    mutation_thread.join(8)
    writer_thread.join(8)

    assert not mutation_thread.is_alive()
    assert not writer_thread.is_alive()
    assert failures == []
    assert progressed_during_await, (
        f"upstream {operation} retained SQLite's writer lock"
    )


def test_superseded_update_attempt_cannot_commit_local_projection(tmp_path) -> None:
    engine = create_engine_from_url(
        f"sqlite:///{tmp_path / 'mutation-fence.sqlite3'}"
    )
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
            metadata={"version": "before"},
        )
        session.commit()

    upstream_started = threading.Event()
    release_upstream = threading.Event()
    failures: list[BaseException] = []

    def mutate() -> None:
        try:
            with session_factory() as session:
                asyncio.run(
                    MemoryService(
                        session=session,
                        mem0=_BlockingMutationClient(
                            "update",
                            upstream_started,
                            release_upstream,
                        ),
                    ).update_memory(
                        project_id=PROJECT_ID,
                        memory_id=MEMORY_ID,
                        request_app_id=APP_ID,
                        payload={"text": "updated"},
                    )
                )
        except BaseException as exc:
            failures.append(exc)

    mutation_thread = threading.Thread(target=mutate, daemon=True)
    mutation_thread.start()
    assert upstream_started.wait(3)
    with session_factory() as session:
        intent = session.scalar(select(MutationIntent))
        assert intent is not None and intent.attempt_count == 1
        intent.attempt_count = 2
        session.commit()
    release_upstream.set()
    mutation_thread.join(8)

    assert not mutation_thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], MutationConflictError)
    with session_factory() as session:
        intent = session.scalar(select(MutationIntent))
        memory = session.scalar(select(MemoryIndex))
        assert intent is not None
        assert (intent.status, intent.attempt_count) == ("ACTIVE", 2)
        assert memory is not None
        assert '"version": "before"' in memory.metadata_projection_json


def test_sqlite_recovery_observation_releases_writer_lock(tmp_path) -> None:
    engine = create_engine_from_url(
        f"sqlite:///{tmp_path / 'recovery-await-release.sqlite3'}"
    )
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

    initial_client = _RecoveryGapClient(threading.Event(), [])
    with session_factory() as session:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                MemoryService(session=session, mem0=initial_client).add_memory(
                    project_id=PROJECT_ID,
                    payload={"text": "ambiguous", "app_id": APP_ID},
                )
            )

    observation_started = threading.Event()
    release_observation = threading.Event()
    writer_done = threading.Event()
    recovery_results: list[dict[str, int]] = []
    failures: list[BaseException] = []

    def recover() -> None:
        try:
            with session_factory() as session:
                recovery_results.append(
                    asyncio.run(
                        MemoryService(
                            session=session,
                            mem0=_BlockingRecoveryObservationClient(
                                initial_client.records,
                                observation_started,
                                release_observation,
                            ),
                        ).recover_pending_mutations(
                            project_id=PROJECT_ID,
                            app_id=APP_ID,
                        )
                    )
                )
        except BaseException as exc:
            failures.append(exc)

    def unrelated_writer() -> None:
        try:
            with session_factory() as session:
                ProjectRepository(session).upsert_default_project(
                    project_id="recovery-unrelated",
                    name="recovery-unrelated",
                    mem0_base_url="http://mem0.invalid",
                )
                session.commit()
                writer_done.set()
        except BaseException as exc:
            failures.append(exc)

    recovery_thread = threading.Thread(target=recover, daemon=True)
    writer_thread = threading.Thread(target=unrelated_writer, daemon=True)
    recovery_thread.start()
    assert observation_started.wait(3)
    writer_thread.start()
    progressed_during_observation = writer_done.wait(0.75)
    release_observation.set()
    recovery_thread.join(8)
    writer_thread.join(8)

    assert failures == []
    assert progressed_during_observation
    assert recovery_results == [{"recovered": 1, "failed": 0}]


def test_independent_app_mutation_progresses_during_upstream_update(tmp_path) -> None:
    engine = create_engine_from_url(
        f"sqlite:///{tmp_path / 'independent-app.sqlite3'}"
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    other_app_id = "independent-app"
    other_memory_id = "independent-memory"
    with session_factory() as session:
        ProjectRepository(session).upsert_default_project(
            project_id=PROJECT_ID,
            name=PROJECT_ID,
            mem0_base_url="http://mem0.invalid",
            default_app_id=APP_ID,
        )
        for memory_id, app_id in (
            (MEMORY_ID, APP_ID),
            (other_memory_id, other_app_id),
        ):
            MemoryIndexRepository(session).upsert_memory(
                project_id=PROJECT_ID,
                mem0_memory_id=memory_id,
                user_id="alice",
                app_id=app_id,
                category=None,
                metadata={},
            )
        session.commit()

    upstream_started = threading.Event()
    release_upstream = threading.Event()
    independent_called = threading.Event()
    failures: list[BaseException] = []

    def slow_update() -> None:
        try:
            with session_factory() as session:
                asyncio.run(
                    MemoryService(
                        session=session,
                        mem0=_BlockingMutationClient(
                            "update",
                            upstream_started,
                            release_upstream,
                        ),
                    ).update_memory(
                        project_id=PROJECT_ID,
                        memory_id=MEMORY_ID,
                        request_app_id=APP_ID,
                        payload={"text": "updated"},
                    )
                )
        except BaseException as exc:
            failures.append(exc)

    def independent_delete() -> None:
        try:
            with session_factory() as session:
                asyncio.run(
                    MemoryService(
                        session=session,
                        mem0=_DeleteClient(independent_called),
                    ).delete_memory(
                        project_id=PROJECT_ID,
                        memory_id=other_memory_id,
                        request_app_id=other_app_id,
                    )
                )
        except BaseException as exc:
            failures.append(exc)

    update_thread = threading.Thread(target=slow_update, daemon=True)
    delete_thread = threading.Thread(target=independent_delete, daemon=True)
    update_thread.start()
    assert upstream_started.wait(3)
    delete_thread.start()
    progressed_during_await = independent_called.wait(0.75)
    delete_thread.join(3)
    release_upstream.set()
    update_thread.join(8)

    assert failures == []
    assert progressed_during_await
    assert not delete_thread.is_alive()
    assert not update_thread.is_alive()


@pytest.mark.parametrize("operation", ["update", "reconcile"])
def test_active_scope_intent_prevents_entity_delete_resurrection_without_lock_wait(
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
    mutation_failures: list[BaseException] = []
    delete_failures: list[BaseException] = []

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
            mutation_failures.append(exc)

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
            delete_failures.append(exc)

    mutation_thread = threading.Thread(target=mutate, daemon=True)
    delete_thread = threading.Thread(target=delete_entity, daemon=True)
    mutation_thread.start()
    assert started.wait(3)
    delete_thread.start()
    assert not delete_called.wait(0.25), (
        f"entity delete bypassed the active {operation} scope intent"
    )
    release.set()
    mutation_thread.join(8)
    delete_thread.join(8)

    assert not mutation_thread.is_alive()
    assert not delete_thread.is_alive()
    assert mutation_failures == []
    assert len(delete_failures) == 1
    assert isinstance(delete_failures[0], MutationConflictError)
    assert not delete_called.is_set()
    with session_factory() as session:
        memory = session.scalar(
            select(MemoryIndex).where(
                MemoryIndex.project_id == PROJECT_ID,
                MemoryIndex.mem0_memory_id == MEMORY_ID,
            )
        )
        assert memory is not None and memory.deleted_at is None
        assert session.scalar(
            select(Entity).where(
                Entity.project_id == PROJECT_ID,
                Entity.app_id == APP_ID,
                Entity.entity_type == "user",
                Entity.entity_id == "alice",
            )
        ) is not None


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
