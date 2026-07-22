import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from threading import Thread
from typing import Any

import pytest
from sqlalchemy import create_engine, event, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from mem0_sidecar.core.explorer_filters import parse_explorer_query
from mem0_sidecar.core.memory_ops import (
    SIDECAR_APP_ID_METADATA_KEY,
    SIDECAR_MUTATION_ID_METADATA_KEY,
    SIDECAR_PROJECT_ID_METADATA_KEY,
    MemoryService,
    MemoryUpstreamProtocolError,
    MutationConflictError,
    extract_memory_id,
    extract_memory_ids,
    memory_content_fingerprint,
    normalize_memory_type,
)
from mem0_sidecar.mem0_client.client import Mem0UpstreamError
from mem0_sidecar.store.models import (
    Base,
    Category,
    Entity,
    EventStatus,
    MemoryIndex,
    MutationIntent,
    Project,
)
from mem0_sidecar.store.repositories import (
    CategoryRepository,
    EntityRepository,
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


class ExplorerMem0Client(FakeMem0Client):
    def __init__(self, records: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.records = dict(records or {})
        self.current_gets = 0
        self.max_concurrent_gets = 0
        self.update_calls: list[tuple[str, dict[str, Any]]] = []
        self.history_calls: list[str] = []
        self.history_response: Any = {"results": []}
        self.list_calls: list[dict[str, Any]] = []
        self.list_response: Any = {"results": []}

    async def get_memory(self, memory_id: str) -> Any:
        self.get_memory_ids.append(memory_id)
        self.current_gets += 1
        self.max_concurrent_gets = max(
            self.max_concurrent_gets,
            self.current_gets,
        )
        try:
            await asyncio.sleep(0.001)
            value = self.records[memory_id]
            if isinstance(value, Exception):
                raise value
            return value
        finally:
            self.current_gets -= 1

    async def update_memory(
        self,
        memory_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.update_calls.append((memory_id, payload))
        record = self.records[memory_id]
        assert isinstance(record, dict)
        if "text" in payload:
            record["memory"] = payload["text"]
        if "metadata" in payload:
            record["metadata"] = payload["metadata"]
        if "expiration_date" in payload:
            record["expiration_date"] = payload["expiration_date"]
        return {"message": "updated"}

    async def get_memory_history(self, memory_id: str) -> Any:
        self.history_calls.append(memory_id)
        if isinstance(self.history_response, Exception):
            raise self.history_response
        return self.history_response

    async def list_memories(self, params: dict[str, Any]) -> Any:
        self.list_calls.append(params)
        if isinstance(self.list_response, Exception):
            raise self.list_response
        return self.list_response


class PausedRefillMem0Client(ExplorerMem0Client):
    def __init__(self, records: dict[str, Any], *, first_batch_size: int) -> None:
        super().__init__(records)
        self.first_batch_size = first_batch_size
        self.later_batch_started = asyncio.Event()
        self.release_later_batch = asyncio.Event()

    async def get_memory(self, memory_id: str) -> Any:
        self.get_memory_ids.append(memory_id)
        self.current_gets += 1
        self.max_concurrent_gets = max(
            self.max_concurrent_gets,
            self.current_gets,
        )
        try:
            if len(self.get_memory_ids) > self.first_batch_size:
                self.later_batch_started.set()
                await self.release_later_batch.wait()
            value = self.records[memory_id]
            if isinstance(value, Exception):
                raise value
            return value
        finally:
            self.current_gets -= 1


class PausedFirstHydrationMem0Client(ExplorerMem0Client):
    def __init__(self, records: dict[str, Any]) -> None:
        super().__init__(records)
        self.hydration_started = asyncio.Event()
        self.release_hydration = asyncio.Event()

    async def get_memory(self, memory_id: str) -> Any:
        self.get_memory_ids.append(memory_id)
        self.hydration_started.set()
        await self.release_hydration.wait()
        return self.records[memory_id]


def _index_memory(
    db_session,
    memory_id: str,
    *,
    project_id: str = "repo-a",
    app_id: str = "app-a",
    metadata: dict[str, Any] | None = None,
    category: str | None = None,
) -> MemoryIndex:
    return MemoryIndexRepository(db_session).upsert_memory(
        project_id=project_id,
        mem0_memory_id=memory_id,
        user_id="root",
        agent_id="codex",
        app_id=app_id,
        run_id="run-1",
        category=category,
        metadata=metadata or {},
    )


def _explorer_query(*, page: int = 1, page_size: int = 20):
    return parse_explorer_query(
        {"page": page, "page_size": page_size, "sort": "created_at_asc"},
        allowed_fields={
            "entity_type",
            "user_id",
            "agent_id",
            "app_id",
            "run_id",
            "memory_id",
            "category",
            "metadata",
        },
    )


def _create_project(
    db_session,
    project_id: str = "repo-a",
    *,
    default_app_id: str = "app-a",
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id=project_id,
        name=project_id,
        mem0_base_url="http://mem0:8000",
        default_app_id=default_app_id,
    )


def _file_sqlite_engine(
    tmp_path,
    name: str,
    *,
    transactional_selects: bool = False,
):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False, "timeout": 0.05},
        future=True,
    )
    if transactional_selects:

        @event.listens_for(engine, "connect")
        def disable_driver_transaction_control(dbapi_connection, _record):
            dbapi_connection.isolation_level = None

        @event.listens_for(engine, "begin")
        def emit_begin(connection):
            connection.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine)
    return engine


def _project_writer_result(engine, project_id: str) -> dict[str, Any]:
    started = time.monotonic()
    try:
        with Session(engine) as session:
            project = session.get(Project, project_id)
            assert project is not None
            project.name = f"updated-{project_id}"
            session.commit()
        return {
            "status": "committed",
            "elapsed": time.monotonic() - started,
            "error": None,
        }
    except OperationalError as exc:
        return {
            "status": "locked",
            "elapsed": time.monotonic() - started,
            "error": str(exc),
        }


async def _run_thread_probe(function, *args, timeout: float = 0.75):
    result: dict[str, Any] = {}

    def run() -> None:
        try:
            result["value"] = function(*args)
        except BaseException as exc:
            result["error"] = exc

    thread = Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    while thread.is_alive() and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    if thread.is_alive():
        raise TimeoutError("thread probe did not finish before timeout")
    if "error" in result:
        raise result["error"]
    return result["value"]


def _projection_writer_result(engine, memory_id: str) -> dict[str, Any]:
    started = time.monotonic()
    try:
        with Session(engine) as session:
            result = session.execute(
                update(MemoryIndex)
                .where(
                    MemoryIndex.project_id == "repo-a",
                    MemoryIndex.app_id == "app-a",
                    MemoryIndex.mem0_memory_id == memory_id,
                    MemoryIndex.deleted_at.is_(None),
                )
                .values(
                    metadata_projection_json='{"revision":"new"}',
                    updated_at=datetime.now(UTC) + timedelta(seconds=1),
                )
            )
            assert result.rowcount == 1
            session.commit()
        return {
            "status": "committed",
            "elapsed": time.monotonic() - started,
            "error": None,
        }
    except OperationalError as exc:
        return {
            "status": "locked",
            "elapsed": time.monotonic() - started,
            "error": str(exc),
        }


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


def test_extract_memory_id_uses_upstream_protocol_error_for_missing_id() -> None:
    with pytest.raises(MemoryUpstreamProtocolError, match="Could not extract"):
        extract_memory_id({"results": [{"memory": "missing-id"}]})


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


def test_fingerprint_normalizes_equivalent_text() -> None:
    left = memory_content_fingerprint({"memory": "Fix\r\n  cache"})
    right = memory_content_fingerprint({"data": "Fix\n cache"})

    assert left == right
    assert left[0] is not None and len(left[0]) == 64
    assert left[1] == len("Fix cache")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("BUGFIX", "bug_fix"),
        ("bugfix", "bug_fix"),
        ("Bug Fix", "bug_fix"),
        ("autoCapture", "auto_capture"),
        (None, "unknown"),
    ],
)
def test_normalize_memory_type_is_stable(value: object, expected: str) -> None:
    assert normalize_memory_type(value) == expected


@pytest.mark.asyncio
async def test_inferred_multi_memory_add_hydrates_each_fingerprint(
    db_session,
) -> None:
    class InferredMem0Client(FakeMem0Client):
        async def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.add_payloads.append(payload)
            return {"results": [{"id": "mem-a"}, {"id": "mem-b"}]}

        async def get_memory(self, memory_id: str) -> dict[str, Any]:
            self.get_memory_ids.append(memory_id)
            return {
                "id": memory_id,
                "memory": f"fact for {memory_id}",
                "metadata": {
                    "type": "BUGFIX",
                    "source": "opencode",
                    "confidence": "0.7",
                },
            }

    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    db_session.commit()
    mem0 = InferredMem0Client()

    await MemoryService(session=db_session, mem0=mem0).add_memory(
        project_id="repo-a",
        payload={"text": "derive facts", "user_id": "root", "app_id": "app-a"},
    )

    projections = {
        item.mem0_memory_id: item
        for item in db_session.query(MemoryIndex).order_by(
            MemoryIndex.mem0_memory_id
        )
    }
    assert mem0.get_memory_ids == ["mem-a", "mem-b"]
    assert projections["mem-a"].content_hash != projections["mem-b"].content_hash
    assert {item.normalized_type for item in projections.values()} == {"bug_fix"}
    assert {item.source for item in projections.values()} == {"opencode"}
    assert all(item.last_observed_at is not None for item in projections.values())


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
    db_session.commit()
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
    mutation_marker = mem0.add_payloads[0]["metadata"].pop(
        SIDECAR_MUTATION_ID_METADATA_KEY
    )
    assert len(mutation_marker) == 64
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
    db_session.commit()
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
    db_session.commit()
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
    db_session.commit()
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
    db_session.commit()

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
async def test_search_memory_trace_is_filtered_correlated_and_durable(
    db_session,
    monkeypatch,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "mem-app-a", app_id="app-a")
    _index_memory(db_session, "mem-app-b", app_id="app-b")
    db_session.commit()
    monkeypatch.setattr(
        "mem0_sidecar.core.memory_ops.get_request_id",
        lambda: "request-search",
    )

    result = await MemoryService(
        session=db_session,
        mem0=ScopedSearchMem0Client(),
    ).search_memories(
        project_id="repo-a",
        payload={"query": "hello", "user_id": "root", "app_id": "app-a"},
    )

    assert result["results"] == [{"id": "mem-app-a", "memory": "hello app a"}]
    assert result["total"] == 1
    with Session(db_session.get_bind()) as verification_session:
        stored = EventRepository(verification_session).list_project_events("repo-a")
    assert len(stored) == 1
    event = stored[0]
    assert event.operation == "memory.search"
    assert event.status is EventStatus.SUCCEEDED
    assert event.correlation_id == "request-search"
    assert event.app_id == "app-a"
    assert event.user_id == "root"
    assert event.result_count == 1
    assert json.loads(event.response_json)["result_previews"] == [
        {"id": "mem-app-a", "memory": "hello app a"}
    ]


@pytest.mark.asyncio
async def test_search_memory_failure_persists_one_event_and_leaves_session_usable(
    db_session,
    monkeypatch,
) -> None:
    _create_project(db_session)
    db_session.commit()
    monkeypatch.setattr(
        "mem0_sidecar.core.memory_ops.get_request_id",
        lambda: "request-search-failed",
    )

    class FailingSearchClient(FakeMem0Client):
        async def search_memories(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.search_payloads.append(payload)
            raise RuntimeError("search failed")

    with pytest.raises(RuntimeError, match="search failed"):
        await MemoryService(
            session=db_session,
            mem0=FailingSearchClient(),
        ).search_memories(
            project_id="repo-a",
            payload={"query": "hello", "app_id": "app-a"},
        )

    _create_project(db_session, "repo-after-search")
    db_session.commit()
    with Session(db_session.get_bind()) as verification_session:
        stored = EventRepository(verification_session).list_project_events("repo-a")
        assert verification_session.get(Project, "repo-after-search") is not None
    assert len(stored) == 1
    assert stored[0].status is EventStatus.FAILED
    assert stored[0].correlation_id == "request-search-failed"


@pytest.mark.asyncio
async def test_add_memory_trace_uses_request_correlation_and_creates_one_event(
    db_session,
    monkeypatch,
) -> None:
    _create_project(db_session)
    db_session.commit()
    monkeypatch.setattr(
        "mem0_sidecar.core.memory_ops.get_request_id",
        lambda: "request-add",
    )

    await MemoryService(session=db_session, mem0=FakeMem0Client()).add_memory(
        project_id="repo-a",
        payload={"text": "remember", "user_id": "root", "app_id": "app-a"},
    )
    db_session.commit()

    stored = EventRepository(db_session).list_project_events("repo-a")
    assert len(stored) == 1
    assert stored[0].operation == "memory.add"
    assert stored[0].correlation_id == "request-add"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["add", "search", "list"])
@pytest.mark.parametrize(
    "write_state",
    ["new", "dirty", "deleted", "flushed", "core"],
)
async def test_traced_memory_operations_reject_unrelated_session_writes_without_commit(
    db_session,
    operation: str,
    write_state: str,
) -> None:
    _create_project(db_session)
    baseline_category = CategoryRepository(db_session).replace_project_categories(
        project_id="repo-a",
        categories=[{"name": "baseline", "description": "keep"}],
    )[0]
    db_session.commit()
    project = db_session.get(Project, "repo-a")
    assert project is not None

    pending_category: Category | None = None
    if write_state == "new":
        pending_category = Category(
            project_id="repo-a",
            name="must-not-commit",
            description="pending",
            schema_json="{}",
        )
        db_session.add(pending_category)
    elif write_state == "dirty":
        project.name = "must-not-commit"
    elif write_state == "deleted":
        db_session.delete(baseline_category)
    elif write_state == "flushed":
        pending_category = Category(
            project_id="repo-a",
            name="must-not-commit",
            description="flushed",
            schema_json="{}",
        )
        db_session.add(pending_category)
        db_session.flush()
    else:
        db_session.execute(
            update(Project)
            .where(Project.id == "repo-a")
            .values(name="must-not-commit")
        )

    mem0 = ExplorerMem0Client()
    service = MemoryService(session=db_session, mem0=mem0)
    with pytest.raises(
        RuntimeError,
        match="traced memory operation requires a clean session write-set",
    ):
        if operation == "add":
            await service.add_memory(
                project_id="repo-a",
                payload={"text": "hello", "app_id": "app-a"},
            )
        elif operation == "search":
            await service.search_memories(
                project_id="repo-a",
                payload={"query": "hello", "app_id": "app-a"},
            )
        else:
            await service.query_memories(
                project_id="repo-a",
                app_id="app-a",
                query=_explorer_query(),
            )

    assert mem0.add_payloads == []
    assert mem0.search_payloads == []
    assert mem0.get_memory_ids == []
    if write_state == "new":
        assert pending_category is not None
        assert pending_category in db_session.new
    elif write_state == "dirty":
        assert project in db_session.dirty
    elif write_state == "deleted":
        assert baseline_category in db_session.deleted
    else:
        assert db_session.in_transaction() is True
        assert not db_session.new
        assert not db_session.dirty
        assert not db_session.deleted

    db_session.rollback()
    with Session(db_session.get_bind()) as verification_session:
        persisted_project = verification_session.get(Project, "repo-a")
        persisted_categories = CategoryRepository(
            verification_session
        ).list_project_categories("repo-a")
    assert persisted_project is not None
    assert persisted_project.name == "repo-a"
    assert [category.name for category in persisted_categories] == ["baseline"]


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
    db_session.commit()

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
    db_session.commit()

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
    db_session.commit()

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
    db_session.commit()

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
    db_session.commit()

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
async def test_memory_service_preserves_exact_special_id_in_projection_and_event(
    db_session,
) -> None:
    memory_id = "part/what?#%é"
    _create_project(db_session)
    _index_memory(db_session, memory_id)
    db_session.commit()
    mem0 = FakeMem0Client()

    result = await MemoryService(session=db_session, mem0=mem0).delete_memory(
        project_id="repo-a",
        memory_id=memory_id,
        request_app_id="app-a",
    )

    assert mem0.deleted_ids == [memory_id]
    event = EventRepository(db_session).get(result["event"]["id"])
    assert event.subject_id == memory_id
    assert json.loads(event.request_json)["memory_id"] == memory_id
    projection = MemoryIndexRepository(db_session).get_memory(
        project_id="repo-a",
        mem0_memory_id=memory_id,
        app_id="app-a",
        include_deleted=True,
    )
    assert projection is not None
    assert projection.mem0_memory_id == memory_id
    assert projection.deleted_at is not None


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
    monkeypatch,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    db_session.commit()
    monkeypatch.setattr(
        "mem0_sidecar.core.memory_ops.get_request_id",
        lambda: "request-add-failed",
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
    assert failed_event[0].correlation_id == "request-add-failed"


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


@pytest.mark.asyncio
async def test_query_memories_hydrates_in_repository_order_with_eight_read_limit(
    db_session,
) -> None:
    _create_project(db_session)
    records: dict[str, Any] = {}
    for index in range(12):
        memory_id = f"mem-{index:02d}"
        _index_memory(db_session, memory_id)
        records[memory_id] = {
            "id": memory_id,
            "memory": f"memory {index}",
            "categories": ["direct", "duplicate"],
            "metadata": {
                "categories": ["duplicate", "metadata"],
                "category": "category",
                "custom_category": "custom",
                "type": "type",
            },
        }
    db_session.commit()
    mem0 = ExplorerMem0Client(records)

    result = await MemoryService(session=db_session, mem0=mem0).query_memories(
        project_id="repo-a",
        app_id="app-a",
        query=_explorer_query(page_size=10),
    )

    assert [item["id"] for item in result["results"]] == [
        f"mem-{index:02d}" for index in range(10)
    ]
    assert result["results"][0]["categories"] == [
        "direct",
        "duplicate",
        "metadata",
        "category",
        "custom",
        "type",
    ]
    assert result == {
        **result,
        "total": 12,
        "page": 1,
        "page_size": 10,
        "stale_skipped": 0,
    }
    assert 1 < mem0.max_concurrent_gets <= 8


@pytest.mark.asyncio
async def test_query_memories_marks_bad_upstream_records_stale_and_fills_page(
    db_session,
) -> None:
    _create_project(db_session)
    records: dict[str, Any] = {
        "mem-0": Mem0UpstreamError(
            method="GET",
            path="/memories/mem-0",
            status_code=404,
            message="missing",
        ),
        "mem-1": {"id": "different-id", "memory": "wrong"},
        "mem-2": {"results": ["malformed"]},
        "mem-3": {"id": "mem-3", "memory": "three"},
        "mem-4": {"id": "mem-4", "memory": "four"},
        "mem-5": {"id": "mem-5", "memory": "five"},
    }
    for memory_id in records:
        _index_memory(db_session, memory_id)
    db_session.commit()

    result = await MemoryService(
        session=db_session,
        mem0=ExplorerMem0Client(records),
    ).query_memories(
        project_id="repo-a",
        app_id="app-a",
        query=_explorer_query(page_size=3),
    )

    assert [item["id"] for item in result["results"]] == [
        "mem-3",
        "mem-4",
        "mem-5",
    ]
    assert result["stale_skipped"] == 3
    assert result["total"] == 3
    for memory_id in ("mem-0", "mem-1", "mem-2"):
        stale = MemoryIndexRepository(db_session).get_memory(
            project_id="repo-a",
            mem0_memory_id=memory_id,
            include_deleted=True,
        )
        assert stale is not None and stale.deleted_at is not None


@pytest.mark.asyncio
async def test_query_memory_trace_uses_exact_entities_and_excludes_cross_app_preview(
    db_session,
    monkeypatch,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "mem-good", app_id="app-a")
    _index_memory(db_session, "mem-conflict", app_id="app-a")
    db_session.commit()
    monkeypatch.setattr(
        "mem0_sidecar.core.memory_ops.get_request_id",
        lambda: "request-list",
    )
    query = parse_explorer_query(
        {
            "match": "all",
            "filters": [
                {"field": "user_id", "operator": "equals", "value": "root"}
            ],
            "page": 1,
            "page_size": 20,
            "sort": "created_at_asc",
        },
        allowed_fields={
            "entity_type",
            "user_id",
            "agent_id",
            "app_id",
            "run_id",
            "memory_id",
            "category",
            "metadata",
        },
    )
    mem0 = ExplorerMem0Client(
        {
            "mem-good": {
                "id": "mem-good",
                "memory": "visible",
                "app_id": "app-a",
                "user_id": "root",
            },
            "mem-conflict": {
                "id": "mem-conflict",
                "memory": "must not leak",
                "app_id": "app-b",
                "user_id": "root",
            },
        }
    )

    result = await MemoryService(session=db_session, mem0=mem0).query_memories(
        project_id="repo-a",
        app_id="app-a",
        query=query,
    )

    assert [item["id"] for item in result["results"]] == ["mem-good"]
    with Session(db_session.get_bind()) as verification_session:
        stored = EventRepository(verification_session).list_project_events("repo-a")
    assert len(stored) == 1
    event = stored[0]
    assert event.operation == "memory.list"
    assert event.status is EventStatus.SUCCEEDED
    assert event.correlation_id == "request-list"
    assert event.app_id == "app-a"
    assert event.user_id == "root"
    assert event.result_count == 1
    previews = json.loads(event.response_json)["result_previews"]
    assert len(previews) == 1
    assert previews[0] == {
        **previews[0],
        "app_id": "app-a",
        "id": "mem-good",
        "memory": "visible",
        "user_id": "root",
    }
    assert "must not leak" not in event.response_json
    assert "app-b" not in event.response_json


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {
            SIDECAR_PROJECT_ID_METADATA_KEY: "repo-a",
            SIDECAR_APP_ID_METADATA_KEY: "app-b",
        },
        {
            SIDECAR_PROJECT_ID_METADATA_KEY: "repo-b",
            SIDECAR_APP_ID_METADATA_KEY: "app-a",
        },
        {SIDECAR_PROJECT_ID_METADATA_KEY: "repo-a"},
        {SIDECAR_APP_ID_METADATA_KEY: "app-a"},
        {
            SIDECAR_PROJECT_ID_METADATA_KEY: 123,
            SIDECAR_APP_ID_METADATA_KEY: "app-a",
        },
    ],
)
async def test_query_memory_rejects_partial_invalid_or_cross_scope_metadata_markers(
    db_session,
    metadata: dict[str, Any],
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "mem-secret", app_id="app-a")
    db_session.commit()
    mem0 = ExplorerMem0Client(
        {
            "mem-secret": {
                "id": "mem-secret",
                "memory": "cross-app-secret-must-not-leak",
                "metadata": metadata,
            }
        }
    )

    result = await MemoryService(session=db_session, mem0=mem0).query_memories(
        project_id="repo-a",
        app_id="app-a",
        query=_explorer_query(),
    )

    assert result["results"] == []
    assert result["total"] == 0
    assert result["stale_skipped"] == 1
    stored = EventRepository(db_session).list_project_events("repo-a")
    assert len(stored) == 1
    event = stored[0]
    assert event.status is EventStatus.SUCCEEDED
    assert event.result_count == 0
    assert "cross-app-secret-must-not-leak" not in event.response_json
    assert "app-b" not in event.response_json
    assert "repo-b" not in event.response_json


@pytest.mark.asyncio
async def test_query_memories_project_wide_preserves_each_projected_app(
    db_session,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "mem-app-a", app_id="app-a")
    _index_memory(db_session, "mem-app-b", app_id="app-b")
    db_session.commit()
    mem0 = ExplorerMem0Client(
        {
            "mem-app-a": {
                "id": "mem-app-a",
                "memory": "from app a",
                "user_id": "root",
                "agent_id": "codex",
                "app_id": "app-a",
                "run_id": "run-1",
                "metadata": {},
            },
            "mem-app-b": {
                "id": "mem-app-b",
                "memory": "from app b",
                "user_id": "root",
                "agent_id": "codex",
                "app_id": "app-b",
                "run_id": "run-1",
                "metadata": {},
            },
        }
    )

    result = await MemoryService(session=db_session, mem0=mem0).query_memories(
        project_id="repo-a",
        app_id=None,
        project_wide=True,
        query=_explorer_query(),
    )

    assert [(item["id"], item["app_id"]) for item in result["results"]] == [
        ("mem-app-a", "app-a"),
        ("mem-app-b", "app-b"),
    ]
    assert result["total"] == 2
    events = EventRepository(db_session).list_project_events("repo-a")
    assert len(events) == 1
    assert events[0].app_id is None


@pytest.mark.asyncio
async def test_project_wide_get_and_update_use_the_memory_projected_app(
    db_session,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "mem-app-b", app_id="app-b")
    db_session.commit()
    mem0 = ExplorerMem0Client(
        {
            "mem-app-b": {
                "id": "mem-app-b",
                "memory": "before",
                "user_id": "root",
                "agent_id": "codex",
                "app_id": "app-b",
                "run_id": "run-1",
                "metadata": {},
            }
        }
    )
    service = MemoryService(session=db_session, mem0=mem0)

    fetched = await service.get_memory(
        project_id="repo-a",
        memory_id="mem-app-b",
        project_wide=True,
    )
    updated = await service.update_memory(
        project_id="repo-a",
        memory_id="mem-app-b",
        request_app_id=None,
        project_wide=True,
        payload={"text": "after", "metadata": {"type": "decision"}},
    )

    assert fetched["app_id"] == "app-b"
    assert updated["memory"]["memory"] == "after"
    assert mem0.update_calls == [
        (
            "mem-app-b",
            {
                "text": "after",
                "metadata": {
                    "type": "decision",
                    SIDECAR_PROJECT_ID_METADATA_KEY: "repo-a",
                    SIDECAR_APP_ID_METADATA_KEY: "app-b",
                },
            },
        )
    ]


@pytest.mark.asyncio
async def test_query_memory_failure_persists_one_event_and_discards_partial_stale(
    db_session,
    monkeypatch,
) -> None:
    _create_project(db_session)
    projection = _index_memory(db_session, "mem-1", app_id="app-a")
    db_session.commit()
    monkeypatch.setattr(
        "mem0_sidecar.core.memory_ops.get_request_id",
        lambda: "request-list-failed",
    )

    with pytest.raises(RuntimeError, match="hydrate failed"):
        await MemoryService(
            session=db_session,
            mem0=ExplorerMem0Client({"mem-1": RuntimeError("hydrate failed")}),
        ).query_memories(
            project_id="repo-a",
            app_id="app-a",
            query=_explorer_query(),
        )

    _create_project(db_session, "repo-after-list")
    db_session.commit()
    with Session(db_session.get_bind()) as verification_session:
        stored = EventRepository(verification_session).list_project_events("repo-a")
        persisted_projection = MemoryIndexRepository(verification_session).get_memory(
            project_id="repo-a",
            mem0_memory_id="mem-1",
            app_id="app-a",
            include_deleted=True,
        )
        assert verification_session.get(Project, "repo-after-list") is not None
    assert projection.deleted_at is None
    assert persisted_projection is not None
    assert persisted_projection.deleted_at is None
    assert len(stored) == 1
    assert stored[0].status is EventStatus.FAILED
    assert stored[0].correlation_id == "request-list-failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"project_id": "repo-b"},
        {"app_id": "app-b"},
        {"user_id": "alice"},
        {"memory_id": "other"},
        {"unknown": "value"},
        {"text": "   "},
        {"text": 123},
        {"metadata": []},
        {"metadata": {f"field-{index}": index for index in range(49)}},
    ],
)
async def test_update_memory_rejects_empty_scope_and_invalid_patch_fields(
    db_session,
    payload: dict[str, Any],
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "mem-1")
    mem0 = ExplorerMem0Client(
        {"mem-1": {"id": "mem-1", "memory": "before", "metadata": {}}}
    )

    with pytest.raises(ValueError):
        await MemoryService(session=db_session, mem0=mem0).update_memory(
            project_id="repo-a",
            memory_id="mem-1",
            request_app_id="app-a",
            payload=payload,
        )

    assert mem0.update_calls == []


@pytest.mark.asyncio
async def test_update_memory_checks_app_scope_before_upstream_access(
    db_session,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "mem-1", app_id="app-a")
    mem0 = ExplorerMem0Client(
        {"mem-1": {"id": "mem-1", "memory": "before", "metadata": {}}}
    )

    with pytest.raises(KeyError):
        await MemoryService(session=db_session, mem0=mem0).update_memory(
            project_id="repo-a",
            memory_id="mem-1",
            request_app_id="app-b",
            payload={"text": "after"},
        )

    assert mem0.update_calls == []
    assert mem0.get_memory_ids == []


@pytest.mark.asyncio
async def test_update_memory_patches_whitelist_and_refreshes_projection(
    db_session,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "mem-1", metadata={"type": "old"})
    mem0 = ExplorerMem0Client(
        {
            "mem-1": {
                "id": "mem-1",
                "memory": "before",
                "user_id": "root",
                "agent_id": "codex",
                "app_id": "app-a",
                "run_id": "run-1",
                "metadata": {"type": "old"},
            }
        }
    )

    result = await MemoryService(session=db_session, mem0=mem0).update_memory(
        project_id="repo-a",
        memory_id="mem-1",
        request_app_id="app-a",
        payload={
            "text": "after",
            "metadata": {"type": "decision"},
            "expiration_date": "2027-01-01T00:00:00Z",
        },
    )

    assert mem0.update_calls == [
        (
            "mem-1",
            {
                "text": "after",
                "metadata": {
                    "type": "decision",
                    SIDECAR_PROJECT_ID_METADATA_KEY: "repo-a",
                    SIDECAR_APP_ID_METADATA_KEY: "app-a",
                },
                "expiration_date": "2027-01-01T00:00:00Z",
            },
        )
    ]
    assert mem0.get_memory_ids == ["mem-1"]
    assert result["memory"]["memory"] == "after"
    assert result["event"]["operation"] == "memory.update"
    projection = MemoryIndexRepository(db_session).get_memory(
        project_id="repo-a",
        mem0_memory_id="mem-1",
        app_id="app-a",
    )
    assert projection is not None
    assert projection.category == "decision"
    assert json.loads(projection.metadata_projection_json)["type"] == "decision"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("history_response", "expected"),
    [
        ([{"event": "ADD"}], [{"event": "ADD"}]),
        ({"results": [{"event": "UPDATE"}]}, [{"event": "UPDATE"}]),
        ({"history": [{"event": "DELETE"}]}, [{"event": "DELETE"}]),
    ],
)
async def test_get_memory_history_checks_scope_and_normalizes_shapes(
    db_session,
    history_response: Any,
    expected: list[dict[str, Any]],
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "mem-1")
    db_session.commit()
    mem0 = ExplorerMem0Client()
    mem0.history_response = history_response
    service = MemoryService(session=db_session, mem0=mem0)

    with pytest.raises(KeyError):
        await service.get_memory_history(
            project_id="repo-a",
            memory_id="mem-1",
            request_app_id="app-b",
        )
    assert mem0.history_calls == []

    result = await service.get_memory_history(
        project_id="repo-a",
        memory_id="mem-1",
        request_app_id="app-a",
    )

    assert result == {"results": expected}
    assert mem0.history_calls == ["mem-1"]


@pytest.mark.asyncio
async def test_detail_and_history_release_read_transaction_during_upstream_wait(
    db_session,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "mem-1")
    db_session.commit()
    transaction_states: list[bool] = []

    class TransactionObservingClient(ExplorerMem0Client):
        async def get_memory(self, memory_id: str) -> dict[str, Any]:
            transaction_states.append(db_session.in_transaction())
            return await super().get_memory(memory_id)

        async def get_memory_history(self, memory_id: str) -> dict[str, Any]:
            transaction_states.append(db_session.in_transaction())
            return await super().get_memory_history(memory_id)

    mem0 = TransactionObservingClient(
        {"mem-1": {"id": "mem-1", "memory": "hello"}}
    )
    mem0.history_response = {"results": [{"event": "ADD"}]}
    service = MemoryService(session=db_session, mem0=mem0)

    await service.get_memory(
        project_id="repo-a",
        memory_id="mem-1",
        request_app_id="app-a",
    )
    await service.get_memory_history(
        project_id="repo-a",
        memory_id="mem-1",
        request_app_id="app-a",
    )

    assert transaction_states == [False, False]
    assert not db_session.in_transaction()


@pytest.mark.asyncio
async def test_get_memory_rechecks_scope_after_upstream_wait(db_session) -> None:
    _create_project(db_session)
    _index_memory(db_session, "mem-1", app_id="app-a")
    db_session.commit()

    class ScopeMovingClient(ExplorerMem0Client):
        async def get_memory(self, memory_id: str) -> dict[str, Any]:
            assert not db_session.in_transaction()
            with Session(db_session.get_bind()) as concurrent_session:
                MemoryIndexRepository(concurrent_session).upsert_memory(
                    project_id="repo-a",
                    mem0_memory_id=memory_id,
                    user_id="other-user",
                    agent_id="other-agent",
                    app_id="app-b",
                    run_id="other-run",
                    category=None,
                    metadata={},
                )
                concurrent_session.commit()
            return {"id": memory_id, "memory": "moved"}

    with pytest.raises((KeyError, MutationConflictError)):
        await MemoryService(
            session=db_session,
            mem0=ScopeMovingClient(),
        ).get_memory(
            project_id="repo-a",
            memory_id="mem-1",
            request_app_id="app-a",
        )

    assert not db_session.in_transaction()


@pytest.mark.asyncio
async def test_reconcile_imports_only_matching_scope_and_marks_absent_stale(
    db_session,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "old-a")
    _index_memory(db_session, "old-b", app_id="app-b")
    matching_metadata = {
        SIDECAR_PROJECT_ID_METADATA_KEY: "repo-a",
        SIDECAR_APP_ID_METADATA_KEY: "app-a",
        "type": "decision",
    }
    mem0 = ExplorerMem0Client()
    mem0.list_response = {
        "results": [
            {"id": "matching", "memory": "match", "metadata": matching_metadata},
            {"id": "unscoped", "memory": "plain", "metadata": {}},
            {
                "id": "other",
                "memory": "other",
                "metadata": {
                    SIDECAR_PROJECT_ID_METADATA_KEY: "repo-b",
                    SIDECAR_APP_ID_METADATA_KEY: "app-a",
                },
            },
            {
                "id": "partial",
                "memory": "partial",
                "metadata": {SIDECAR_PROJECT_ID_METADATA_KEY: "repo-a"},
            },
        ]
    }

    result = await MemoryService(session=db_session, mem0=mem0).reconcile_memories(
        project_id="repo-a",
        app_id="app-a",
        adopt_unscoped=False,
        allow_adopt_unscoped=False,
        default_project_id="repo-a",
    )

    assert result == {
        "scanned": 4,
        "indexed": 1,
        "skipped_unscoped": 1,
        "skipped_other_scope": 2,
        "stale_marked": 1,
    }
    assert mem0.list_calls == [{"top_k": 5000, "show_expired": True}]
    assert (
        MemoryIndexRepository(db_session).get_memory(
            project_id="repo-a",
            mem0_memory_id="matching",
            app_id="app-a",
        )
        is not None
    )
    stale = MemoryIndexRepository(db_session).get_memory(
        project_id="repo-a",
        mem0_memory_id="old-a",
        include_deleted=True,
    )
    assert stale is not None and stale.deleted_at is not None
    assert (
        MemoryIndexRepository(db_session).get_memory(
            project_id="repo-a",
            mem0_memory_id="old-b",
            app_id="app-b",
        )
        is not None
    )


@pytest.mark.asyncio
async def test_direct_write_mirror_preserves_source_app_identity_and_repairs_scope(
    db_session,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "raw-app", app_id="wrong-default")
    db_session.commit()
    mem0 = ExplorerMem0Client()
    mem0.list_response = {
        "results": [
            {
                "id": "marked",
                "user_id": "root",
                "metadata": {
                    SIDECAR_PROJECT_ID_METADATA_KEY: "repo-a",
                    SIDECAR_APP_ID_METADATA_KEY: "marked-app",
                },
            },
            {"id": "raw-app", "app_id": "raw-source", "metadata": {}},
            {"id": "metadata-app", "metadata": {"app_id": "metadata-source"}},
            {
                "id": "blank-raw-app",
                "app_id": "",
                "metadata": {"app_id": "metadata-after-blank"},
            },
            {
                "id": "blank-app-fallback",
                "app_id": "",
                "metadata": {"app_id": ""},
            },
            {"id": "fallback", "metadata": {}},
            {
                "id": "foreign",
                "metadata": {
                    SIDECAR_PROJECT_ID_METADATA_KEY: "repo-b",
                    SIDECAR_APP_ID_METADATA_KEY: "foreign-app",
                },
            },
            {
                "id": "partial-marker",
                "metadata": {SIDECAR_PROJECT_ID_METADATA_KEY: "repo-a"},
            },
        ],
        "total": 8,
    }

    result = await MemoryService(
        session=db_session,
        mem0=mem0,
    ).mirror_direct_writes(
        project_id="repo-a",
        default_app_id="default-app",
        scan_limit=100,
    )

    assert result == {
        "scanned": 8,
        "indexed": 6,
        "skipped_foreign": 1,
        "skipped_invalid": 1,
        "stale_marked": 0,
        "truncated": False,
    }
    assert mem0.list_calls == [{"top_k": 100, "show_expired": True}]
    expected_apps = {
        "marked": "marked-app",
        "raw-app": "raw-source",
        "metadata-app": "metadata-source",
        "blank-raw-app": "metadata-after-blank",
        "blank-app-fallback": "default-app",
        "fallback": "default-app",
    }
    for memory_id, app_id in expected_apps.items():
        projection = MemoryIndexRepository(db_session).get_memory(
            project_id="repo-a",
            mem0_memory_id=memory_id,
            app_id=app_id,
        )
        assert projection is not None
    assert (
        MemoryIndexRepository(db_session).get_memory(
            project_id="repo-a",
            mem0_memory_id="raw-app",
            app_id="wrong-default",
        )
        is None
    )


@pytest.mark.asyncio
async def test_direct_write_mirror_blank_app_fallback_hydrates_and_rejects_falsey(
    db_session,
) -> None:
    _create_project(db_session)
    records = {
        "blank-metadata": {
            "id": "blank-metadata",
            "app_id": "",
            "metadata": {"app_id": "metadata-app"},
        },
        "blank-default": {
            "id": "blank-default",
            "app_id": "",
            "metadata": {"app_id": ""},
        },
        "false-app": {"id": "false-app", "app_id": False, "metadata": {}},
        "list-app": {"id": "list-app", "app_id": [], "metadata": {}},
    }
    mem0 = ExplorerMem0Client(records)
    mem0.list_response = {"results": list(records.values()), "total": 4}
    service = MemoryService(session=db_session, mem0=mem0)

    mirrored = await service.mirror_direct_writes(
        project_id="repo-a",
        default_app_id="default-app",
        scan_limit=100,
    )
    listed = await service.query_memories(
        project_id="repo-a",
        app_id=None,
        project_wide=True,
        query=_explorer_query(),
    )

    assert mirrored == {
        "scanned": 4,
        "indexed": 2,
        "skipped_foreign": 0,
        "skipped_invalid": 2,
        "stale_marked": 0,
        "truncated": False,
    }
    assert sorted(
        (memory["id"], memory["app_id"]) for memory in listed["results"]
    ) == [
        ("blank-default", "default-app"),
        ("blank-metadata", "metadata-app"),
    ]


@pytest.mark.asyncio
async def test_direct_write_mirror_never_marks_stale_after_truncated_scan(
    db_session,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "must-remain", app_id="app-a")
    db_session.commit()
    mem0 = ExplorerMem0Client()
    mem0.list_response = {
        "results": [
            {"id": "seen-a", "app_id": "app-a", "metadata": {}},
            {"id": "seen-b", "app_id": "app-b", "metadata": {}},
        ],
        "total": 3,
    }

    result = await MemoryService(
        session=db_session,
        mem0=mem0,
    ).mirror_direct_writes(
        project_id="repo-a",
        default_app_id="default-app",
        scan_limit=2,
    )

    assert result["truncated"] is True
    assert result["stale_marked"] == 0
    assert (
        MemoryIndexRepository(db_session).get_memory(
            project_id="repo-a",
            mem0_memory_id="must-remain",
            app_id="app-a",
        )
        is not None
    )


@pytest.mark.asyncio
async def test_direct_write_mirror_treats_legacy_cap_as_incomplete_without_total(
    db_session,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "must-remain", app_id="app-a")
    db_session.commit()
    mem0 = ExplorerMem0Client()
    mem0.list_response = {
        "results": [
            {"id": f"seen-{index}", "app_id": "app-a", "metadata": {}}
            for index in range(3)
        ]
    }

    result = await MemoryService(
        session=db_session,
        mem0=mem0,
    ).mirror_direct_writes(
        project_id="repo-a",
        default_app_id="default-app",
        scan_limit=5,
        legacy_cap=3,
    )

    assert result["truncated"] is True
    assert result["stale_marked"] == 0
    assert (
        MemoryIndexRepository(db_session).get_memory(
            project_id="repo-a",
            mem0_memory_id="must-remain",
            app_id="app-a",
        )
        is not None
    )


@pytest.mark.asyncio
async def test_direct_write_mirror_complete_scan_stales_missing_across_apps(
    db_session,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "keep", app_id="app-a")
    _index_memory(db_session, "stale-a", app_id="app-a")
    _index_memory(db_session, "stale-b", app_id="app-b")
    db_session.commit()
    mem0 = ExplorerMem0Client()
    mem0.list_response = {
        "results": [{"id": "keep", "app_id": "app-a", "metadata": {}}],
        "total": 1,
    }

    result = await MemoryService(
        session=db_session,
        mem0=mem0,
    ).mirror_direct_writes(
        project_id="repo-a",
        default_app_id="default-app",
        scan_limit=100,
    )

    assert result["truncated"] is False
    assert result["stale_marked"] == 2
    for memory_id in ("stale-a", "stale-b"):
        projection = MemoryIndexRepository(db_session).get_memory(
            project_id="repo-a",
            mem0_memory_id=memory_id,
            include_deleted=True,
        )
        assert projection is not None and projection.deleted_at is not None


@pytest.mark.asyncio
async def test_reconcile_stale_cleanup_is_keyset_bounded_and_incremental(
    db_session,
    monkeypatch,
) -> None:
    _create_project(db_session)
    for index in range(501):
        _index_memory(db_session, f"local-{index:04d}")
    EntityRepository(db_session).rebuild_project_entities("repo-a", "app-a")
    db_session.commit()

    batch_sizes: list[int] = []
    original_mark_stale = MemoryIndexRepository.mark_stale_if_unchanged

    def bounded_mark_stale(self, **kwargs):
        memory_ids = list(kwargs["mem0_memory_ids"])
        batch_sizes.append(len(memory_ids))
        assert len(memory_ids) <= 200
        return original_mark_stale(
            self,
            **{**kwargs, "mem0_memory_ids": memory_ids},
        )

    monkeypatch.setattr(
        MemoryIndexRepository,
        "mark_stale_if_unchanged",
        bounded_mark_stale,
    )
    monkeypatch.setattr(
        MemoryIndexRepository,
        "query_project_memories",
        lambda *_args, **_kwargs: pytest.fail(
            "reconcile stale cleanup must use a keyset scan"
        ),
    )
    monkeypatch.setattr(
        EntityRepository,
        "rebuild_project_entities",
        lambda *_args, **_kwargs: pytest.fail(
            "reconcile must refresh only affected entity identities"
        ),
    )
    mem0 = ExplorerMem0Client()
    mem0.list_response = {"results": [], "total": 0}

    result = await MemoryService(session=db_session, mem0=mem0).reconcile_memories(
        project_id="repo-a",
        app_id="app-a",
        adopt_unscoped=False,
        allow_adopt_unscoped=False,
        default_project_id="repo-a",
    )

    assert result["stale_marked"] == 501
    assert batch_sizes == [200, 200, 101]
    assert (
        db_session.query(MemoryIndex)
        .filter(
            MemoryIndex.project_id == "repo-a",
            MemoryIndex.app_id == "app-a",
            MemoryIndex.deleted_at.is_not(None),
        )
        .count()
        == 501
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allow_adopt", "project_id", "default_project_id"),
    [(False, "repo-a", "repo-a"), (True, "repo-b", "repo-a")],
)
async def test_reconcile_rejects_unscoped_adoption_without_all_gates(
    db_session,
    allow_adopt: bool,
    project_id: str,
    default_project_id: str,
) -> None:
    _create_project(db_session, project_id)
    mem0 = ExplorerMem0Client()

    with pytest.raises(ValueError):
        await MemoryService(session=db_session, mem0=mem0).reconcile_memories(
            project_id=project_id,
            app_id="app-a",
            adopt_unscoped=True,
            allow_adopt_unscoped=allow_adopt,
            default_project_id=default_project_id,
        )

    assert mem0.list_calls == []


@pytest.mark.asyncio
async def test_reconcile_adopts_unscoped_only_with_runtime_opt_in_on_default_project(
    db_session,
) -> None:
    _create_project(db_session)
    source = {"id": "unscoped", "memory": "plain", "metadata": {"type": "note"}}
    mem0 = ExplorerMem0Client()
    mem0.list_response = {"results": [source]}

    result = await MemoryService(session=db_session, mem0=mem0).reconcile_memories(
        project_id="repo-a",
        app_id="app-a",
        adopt_unscoped=True,
        allow_adopt_unscoped=True,
        default_project_id="repo-a",
    )

    assert result == {
        "scanned": 1,
        "indexed": 1,
        "skipped_unscoped": 0,
        "skipped_other_scope": 0,
        "stale_marked": 0,
    }
    adopted = MemoryIndexRepository(db_session).get_memory(
        project_id="repo-a",
        mem0_memory_id="unscoped",
        app_id="app-a",
    )
    assert adopted is not None and adopted.category == "note"
    assert source["metadata"] == {"type": "note"}
    assert mem0.update_calls == []


@pytest.mark.asyncio
async def test_reconcile_does_not_mark_absent_stale_when_scan_hits_limit(
    db_session,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "must-remain-active")
    mem0 = ExplorerMem0Client()
    mem0.list_response = {
        "results": [
            {
                "id": f"other-{index}",
                "metadata": {
                    SIDECAR_PROJECT_ID_METADATA_KEY: "repo-b",
                    SIDECAR_APP_ID_METADATA_KEY: "app-b",
                },
            }
            for index in range(5000)
        ]
    }

    result = await MemoryService(session=db_session, mem0=mem0).reconcile_memories(
        project_id="repo-a",
        app_id="app-a",
        adopt_unscoped=False,
        allow_adopt_unscoped=False,
        default_project_id="repo-a",
    )

    assert result["scanned"] == 5000
    assert result["stale_marked"] == 0
    assert (
        MemoryIndexRepository(db_session).get_memory(
            project_id="repo-a",
            mem0_memory_id="must-remain-active",
            app_id="app-a",
        )
        is not None
    )


@pytest.mark.asyncio
async def test_reconcile_rejects_results_beyond_requested_scan_limit(
    db_session,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "must-remain-active")
    mem0 = ExplorerMem0Client()
    mem0.list_response = {
        "results": [
            {
                "id": f"other-{index}",
                "metadata": {
                    SIDECAR_PROJECT_ID_METADATA_KEY: "repo-b",
                    SIDECAR_APP_ID_METADATA_KEY: "app-b",
                },
            }
            for index in range(5001)
        ],
        "total": 5001,
    }

    with pytest.raises(MemoryUpstreamProtocolError, match="scan limit"):
        await MemoryService(session=db_session, mem0=mem0).reconcile_memories(
            project_id="repo-a",
            app_id="app-a",
            adopt_unscoped=False,
            allow_adopt_unscoped=False,
            default_project_id="repo-a",
        )

    assert (
        MemoryIndexRepository(db_session).get_memory(
            project_id="repo-a",
            mem0_memory_id="must-remain-active",
            app_id="app-a",
        )
        is not None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "envelope",
    [{}, {"results": {}}, {"unexpected": []}],
)
async def test_reconcile_rejects_malformed_envelope_without_stale_cleanup(
    db_session,
    envelope: dict[str, Any],
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "must-remain-active")
    mem0 = ExplorerMem0Client()
    mem0.list_response = envelope

    with pytest.raises(MemoryUpstreamProtocolError, match="list response"):
        await MemoryService(session=db_session, mem0=mem0).reconcile_memories(
            project_id="repo-a",
            app_id="app-a",
            adopt_unscoped=False,
            allow_adopt_unscoped=False,
            default_project_id="repo-a",
        )

    assert (
        MemoryIndexRepository(db_session).get_memory(
            project_id="repo-a",
            mem0_memory_id="must-remain-active",
            app_id="app-a",
        )
        is not None
    )


@pytest.mark.asyncio
async def test_reconcile_wraps_list_decode_value_error_with_cause(db_session) -> None:
    _create_project(db_session)
    decode_error = ValueError("list response is not JSON")
    mem0 = ExplorerMem0Client()
    mem0.list_response = decode_error

    with pytest.raises(MemoryUpstreamProtocolError) as exc_info:
        await MemoryService(session=db_session, mem0=mem0).reconcile_memories(
            project_id="repo-a",
            app_id="app-a",
            adopt_unscoped=False,
            allow_adopt_unscoped=False,
            default_project_id="repo-a",
        )

    assert exc_info.value.__cause__ is decode_error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "envelope",
    [
        {"results": [], "total": "unknown"},
        {"results": [{"id": "one", "metadata": {}}], "total": 0},
    ],
)
async def test_reconcile_rejects_untrustworthy_total_without_stale_cleanup(
    db_session,
    envelope: dict[str, Any],
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "must-remain-active")
    mem0 = ExplorerMem0Client()
    mem0.list_response = envelope

    with pytest.raises(MemoryUpstreamProtocolError, match="total"):
        await MemoryService(session=db_session, mem0=mem0).reconcile_memories(
            project_id="repo-a",
            app_id="app-a",
            adopt_unscoped=False,
            allow_adopt_unscoped=False,
            default_project_id="repo-a",
        )

    assert (
        MemoryIndexRepository(db_session).get_memory(
            project_id="repo-a",
            mem0_memory_id="must-remain-active",
            app_id="app-a",
        )
        is not None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record",
    [None, {"memory": "missing id"}, {"id": "bad", "metadata": []}],
)
async def test_reconcile_rejects_malformed_record_without_stale_cleanup(
    db_session,
    record: Any,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "must-remain-active")
    mem0 = ExplorerMem0Client()
    mem0.list_response = {"results": [record]}

    with pytest.raises(MemoryUpstreamProtocolError, match="record"):
        await MemoryService(session=db_session, mem0=mem0).reconcile_memories(
            project_id="repo-a",
            app_id="app-a",
            adopt_unscoped=False,
            allow_adopt_unscoped=False,
            default_project_id="repo-a",
        )

    assert (
        MemoryIndexRepository(db_session).get_memory(
            project_id="repo-a",
            mem0_memory_id="must-remain-active",
            app_id="app-a",
        )
        is not None
    )


@pytest.mark.asyncio
async def test_get_memory_history_rejects_unknown_response_shape(db_session) -> None:
    _create_project(db_session)
    _index_memory(db_session, "mem-1")
    db_session.commit()
    mem0 = ExplorerMem0Client()
    mem0.history_response = {"unexpected": []}

    with pytest.raises(MemoryUpstreamProtocolError, match="history response"):
        await MemoryService(session=db_session, mem0=mem0).get_memory_history(
            project_id="repo-a",
            memory_id="mem-1",
            request_app_id="app-a",
        )


@pytest.mark.asyncio
async def test_get_memory_history_wraps_decode_value_error_with_cause(
    db_session,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "mem-1")
    db_session.commit()
    decode_error = ValueError("history response is not JSON")
    mem0 = ExplorerMem0Client()
    mem0.history_response = decode_error

    with pytest.raises(MemoryUpstreamProtocolError) as exc_info:
        await MemoryService(session=db_session, mem0=mem0).get_memory_history(
            project_id="repo-a",
            memory_id="mem-1",
            request_app_id="app-a",
        )

    assert exc_info.value.__cause__ is decode_error


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [{}, None])
async def test_update_memory_can_clear_projection_category(
    db_session,
    metadata: dict[str, Any] | None,
) -> None:
    _create_project(db_session)
    _index_memory(
        db_session,
        "mem-1",
        metadata={"type": "old-category"},
        category="old-category",
    )
    mem0 = ExplorerMem0Client(
        {
            "mem-1": {
                "id": "mem-1",
                "memory": "before",
                "metadata": {"type": "old-category"},
            }
        }
    )

    result = await MemoryService(session=db_session, mem0=mem0).update_memory(
        project_id="repo-a",
        memory_id="mem-1",
        request_app_id="app-a",
        payload={"metadata": metadata},
    )

    projection = MemoryIndexRepository(db_session).get_memory(
        project_id="repo-a",
        mem0_memory_id="mem-1",
        app_id="app-a",
    )
    assert projection is not None
    assert projection.category is None
    assert result["memory"]["categories"] == []


@pytest.mark.asyncio
async def test_reconcile_does_not_stale_projection_updated_during_scan(
    db_session,
) -> None:
    _create_project(db_session)
    projection = _index_memory(db_session, "concurrent-update")
    original_updated_at = projection.updated_at

    class UpdatingListClient(ExplorerMem0Client):
        async def list_memories(self, params: dict[str, Any]) -> Any:
            self.list_calls.append(params)
            MemoryIndexRepository(db_session).upsert_memory(
                project_id="repo-a",
                mem0_memory_id="concurrent-update",
                user_id="root",
                agent_id="codex",
                app_id="app-a",
                run_id="run-1",
                category=None,
                metadata={"updated": "during-reconcile"},
            )
            db_session.commit()
            return {"results": []}

    result = await MemoryService(
        session=db_session,
        mem0=UpdatingListClient(
            {
                "concurrent-update": {
                    "id": "concurrent-update",
                    "memory": "before",
                    "metadata": {},
                }
            }
        ),
    ).reconcile_memories(
        project_id="repo-a",
        app_id="app-a",
        adopt_unscoped=False,
        allow_adopt_unscoped=False,
        default_project_id="repo-a",
    )

    assert result["stale_marked"] == 0
    assert projection.deleted_at is None
    projection_updated_at = projection.updated_at
    if projection_updated_at.tzinfo is None:
        projection_updated_at = projection_updated_at.replace(tzinfo=UTC)
    assert projection_updated_at > original_updated_at


@pytest.mark.asyncio
async def test_reconcile_does_not_overwrite_projection_updated_during_scan(
    db_session,
) -> None:
    _create_project(db_session)
    projection = _index_memory(
        db_session,
        "concurrent-accepted",
        metadata={"version": "before"},
    )

    class UpdatingListClient(ExplorerMem0Client):
        async def list_memories(self, params: dict[str, Any]) -> Any:
            self.list_calls.append(params)
            MemoryIndexRepository(db_session).upsert_memory(
                project_id="repo-a",
                mem0_memory_id="concurrent-accepted",
                user_id="root",
                agent_id="codex",
                app_id="app-a",
                run_id="run-1",
                category=None,
                metadata={"version": "concurrent"},
            )
            db_session.commit()
            return {
                "results": [
                    {
                        "id": "concurrent-accepted",
                        "memory": "upstream snapshot",
                        "metadata": {
                            SIDECAR_PROJECT_ID_METADATA_KEY: "repo-a",
                            SIDECAR_APP_ID_METADATA_KEY: "app-a",
                            "version": "stale-upstream-scan",
                        },
                    }
                ],
                "total": 1,
            }

    result = await MemoryService(
        session=db_session,
        mem0=UpdatingListClient(),
    ).reconcile_memories(
        project_id="repo-a",
        app_id="app-a",
        adopt_unscoped=False,
        allow_adopt_unscoped=False,
        default_project_id="repo-a",
    )

    assert result["indexed"] == 1
    assert json.loads(projection.metadata_projection_json) == {
        "version": "concurrent"
    }
    assert projection.deleted_at is None


@pytest.mark.asyncio
async def test_reconcile_does_not_reassign_active_projection_during_adoption(
    db_session,
) -> None:
    _create_project(db_session)
    existing = _index_memory(db_session, "shared", app_id="app-a")
    mem0 = ExplorerMem0Client()
    mem0.list_response = {
        "results": [{"id": "shared", "memory": "unscoped", "metadata": {}}]
    }

    result = await MemoryService(session=db_session, mem0=mem0).reconcile_memories(
        project_id="repo-a",
        app_id="app-b",
        adopt_unscoped=True,
        allow_adopt_unscoped=True,
        default_project_id="repo-a",
    )

    assert result == {
        "scanned": 1,
        "indexed": 0,
        "skipped_unscoped": 0,
        "skipped_other_scope": 1,
        "stale_marked": 0,
    }
    assert existing.app_id == "app-a"
    assert (
        MemoryIndexRepository(db_session).get_memory(
            project_id="repo-a",
            mem0_memory_id="shared",
            app_id="app-b",
        )
        is None
    )


@pytest.mark.asyncio
async def test_reconcile_uses_atomic_claim_when_competing_projection_appears(
    db_session,
    monkeypatch,
) -> None:
    _create_project(db_session)
    mem0 = ExplorerMem0Client()
    mem0.list_response = {
        "results": [{"id": "raced", "memory": "unscoped", "metadata": {}}]
    }
    original_claim = MemoryIndexRepository.claim_memory
    claim_calls = 0

    def competing_claim(self, **kwargs):
        nonlocal claim_calls
        claim_calls += 1
        self.upsert_memory(
            project_id="repo-a",
            mem0_memory_id="raced",
            user_id="alice",
            agent_id=None,
            app_id="app-a",
            run_id=None,
            category=None,
            metadata={},
        )
        return original_claim(self, **kwargs)

    monkeypatch.setattr(MemoryIndexRepository, "claim_memory", competing_claim)

    result = await MemoryService(session=db_session, mem0=mem0).reconcile_memories(
        project_id="repo-a",
        app_id="app-b",
        adopt_unscoped=True,
        allow_adopt_unscoped=True,
        default_project_id="repo-a",
    )

    assert claim_calls == 1
    assert result["indexed"] == 0
    assert result["skipped_other_scope"] == 1
    projection = MemoryIndexRepository(db_session).get_memory(
        project_id="repo-a",
        mem0_memory_id="raced",
    )
    assert projection is not None
    assert projection.app_id == "app-a"


@pytest.mark.asyncio
async def test_query_memories_releases_sqlite_writer_lock_before_refill_await(
    tmp_path,
) -> None:
    engine = _file_sqlite_engine(tmp_path, "refill-writer-lock.sqlite3")
    stale_count = 42
    records: dict[str, Any] = {}
    query_task: asyncio.Task[dict[str, Any]] | None = None
    mem0: PausedRefillMem0Client | None = None
    try:
        with Session(engine) as seed_session:
            _create_project(seed_session)
            _create_project(
                seed_session,
                "repo-unrelated",
                default_app_id="app-unrelated",
            )
            for index in range(stale_count):
                memory_id = f"stale-{index:02d}"
                _index_memory(seed_session, memory_id)
                records[memory_id] = Mem0UpstreamError(
                    method="GET",
                    path=f"/memories/{memory_id}",
                    status_code=404,
                    message="missing",
                )
            seed_session.commit()

        mem0 = PausedRefillMem0Client(records, first_batch_size=21)
        with Session(engine, expire_on_commit=False) as query_session:
            query_task = asyncio.create_task(
                MemoryService(session=query_session, mem0=mem0).query_memories(
                    project_id="repo-a",
                    app_id="app-a",
                    query=_explorer_query(page_size=1),
                )
            )
            await asyncio.wait_for(mem0.later_batch_started.wait(), timeout=1)
            assert len(mem0.get_memory_ids) > 21
            assert not query_task.done()
            assert not query_session.in_transaction(), (
                "query session retained a transaction during refill hydration"
            )

            writer_result = await _run_thread_probe(
                _project_writer_result,
                engine,
                "repo-unrelated",
            )

            mem0.release_later_batch.set()
            query_result = await asyncio.wait_for(query_task, timeout=2)
            query_task = None

        assert writer_result["status"] == "committed", writer_result
        assert writer_result["elapsed"] < 0.75, writer_result
        assert query_result["results"] == []
        assert query_result["stale_skipped"] == stale_count
    finally:
        if mem0 is not None:
            mem0.release_later_batch.set()
        if query_task is not None:
            try:
                await asyncio.wait_for(query_task, timeout=2)
            except (Exception, asyncio.CancelledError):
                pass
        engine.dispose()


@pytest.mark.asyncio
async def test_query_memories_releases_sqlite_read_before_first_hydration_await(
    tmp_path,
) -> None:
    engine = _file_sqlite_engine(
        tmp_path,
        "first-hydration-read-lock.sqlite3",
        transactional_selects=True,
    )
    memory_id = "mem-first"
    query_task: asyncio.Task[dict[str, Any]] | None = None
    mem0 = PausedFirstHydrationMem0Client(
        {memory_id: {"id": memory_id, "memory": "first"}}
    )
    try:
        with Session(engine) as seed_session:
            _create_project(seed_session)
            _create_project(
                seed_session,
                "repo-unrelated",
                default_app_id="app-unrelated",
            )
            _index_memory(seed_session, memory_id)
            seed_session.commit()

        with Session(engine, expire_on_commit=False) as query_session:
            query_task = asyncio.create_task(
                MemoryService(session=query_session, mem0=mem0).query_memories(
                    project_id="repo-a",
                    app_id="app-a",
                    query=_explorer_query(page_size=1),
                )
            )
            await asyncio.wait_for(mem0.hydration_started.wait(), timeout=1)
            assert not query_task.done()
            assert not query_session.in_transaction(), (
                "query session retained a transaction during first hydration"
            )

            writer_result = await _run_thread_probe(
                _project_writer_result,
                engine,
                "repo-unrelated",
            )

            mem0.release_hydration.set()
            query_result = await asyncio.wait_for(query_task, timeout=2)
            query_task = None

        assert writer_result["status"] == "committed", writer_result
        assert writer_result["elapsed"] < 0.75, writer_result
        assert [item["id"] for item in query_result["results"]] == [memory_id]
    finally:
        mem0.release_hydration.set()
        if query_task is not None:
            try:
                await asyncio.wait_for(query_task, timeout=2)
            except (Exception, asyncio.CancelledError):
                pass
        engine.dispose()


@pytest.mark.asyncio
async def test_query_memories_never_returns_cached_payload_after_version_race(
    tmp_path,
) -> None:
    engine = _file_sqlite_engine(tmp_path, "refill-cache-race.sqlite3")
    cached_id = "mem-cached"
    first_batch_size = 22
    records: dict[str, Any] = {}
    query_task: asyncio.Task[dict[str, Any]] | None = None
    mem0: PausedRefillMem0Client | None = None
    try:
        with Session(engine) as seed_session:
            _create_project(seed_session)
            for index in range(21):
                memory_id = f"stale-first-{index:02d}"
                _index_memory(seed_session, memory_id)
                records[memory_id] = Mem0UpstreamError(
                    method="GET",
                    path=f"/memories/{memory_id}",
                    status_code=404,
                    message="missing",
                )
            _index_memory(seed_session, cached_id)
            records[cached_id] = {"id": cached_id, "memory": "cached-old"}
            _index_memory(seed_session, "stale-later")
            records["stale-later"] = Mem0UpstreamError(
                method="GET",
                path="/memories/stale-later",
                status_code=404,
                message="missing",
            )
            _index_memory(seed_session, "mem-tail")
            records["mem-tail"] = {"id": "mem-tail", "memory": "tail"}
            seed_session.commit()

        mem0 = PausedRefillMem0Client(
            records,
            first_batch_size=first_batch_size,
        )
        with Session(engine, expire_on_commit=False) as query_session:
            query_task = asyncio.create_task(
                MemoryService(session=query_session, mem0=mem0).query_memories(
                    project_id="repo-a",
                    app_id="app-a",
                    query=_explorer_query(page_size=2),
                )
            )
            await asyncio.wait_for(mem0.later_batch_started.wait(), timeout=1)
            first_batch_ids = mem0.get_memory_ids[:first_batch_size]
            assert cached_id in first_batch_ids
            assert any(
                memory_id.startswith("stale-first-") for memory_id in first_batch_ids
            )
            assert mem0.get_memory_ids.count(cached_id) == 1
            assert not query_task.done()

            writer_result = await _run_thread_probe(
                _projection_writer_result,
                engine,
                cached_id,
            )
            if writer_result["status"] == "committed":
                mem0.records[cached_id] = {
                    "id": cached_id,
                    "memory": "cache-race-new",
                }

            mem0.release_later_batch.set()
            query_result: dict[str, Any] | None = None
            query_error: MutationConflictError | None = None
            try:
                query_result = await asyncio.wait_for(query_task, timeout=2)
            except MutationConflictError as exc:
                query_error = exc
            query_task = None

        assert writer_result["status"] == "committed", writer_result
        assert writer_result["elapsed"] < 0.75, writer_result
        if query_error is not None:
            assert "retry" in str(query_error).lower()
        else:
            assert query_result is not None
            returned = {item["id"]: item["memory"] for item in query_result["results"]}
            assert returned[cached_id] == "cache-race-new", {
                "returned": returned,
                "gets": mem0.get_memory_ids,
            }
            assert mem0.get_memory_ids.count(cached_id) >= 2
    finally:
        if mem0 is not None:
            mem0.release_later_batch.set()
        if query_task is not None:
            try:
                await asyncio.wait_for(query_task, timeout=2)
            except (Exception, asyncio.CancelledError):
                pass
        engine.dispose()


@pytest.mark.asyncio
async def test_query_memories_refills_after_deep_stale_prefix(
    db_session,
    monkeypatch,
) -> None:
    _create_project(db_session)
    records: dict[str, Any] = {}
    stale_count = 23
    valid_count = 5
    for index in range(stale_count + valid_count):
        memory_id = f"mem-{index:02d}"
        _index_memory(db_session, memory_id)
        if index < stale_count:
            records[memory_id] = Mem0UpstreamError(
                method="GET",
                path=f"/memories/{memory_id}",
                status_code=404,
                message="missing",
            )
        else:
            records[memory_id] = {"id": memory_id, "memory": "valid"}
    db_session.commit()
    mem0 = ExplorerMem0Client(records)
    observed_offsets: list[int | None] = []
    original_query = MemoryIndexRepository.query_project_memories

    def capture_offset(self, *args, window_offset=None, **kwargs):
        observed_offsets.append(window_offset)
        return original_query(
            self,
            *args,
            window_offset=window_offset,
            **kwargs,
        )

    monkeypatch.setattr(
        MemoryIndexRepository,
        "query_project_memories",
        capture_offset,
    )

    result = await MemoryService(session=db_session, mem0=mem0).query_memories(
        project_id="repo-a",
        app_id="app-a",
        query=_explorer_query(page_size=3),
    )

    assert [item["id"] for item in result["results"]] == [
        "mem-23",
        "mem-24",
        "mem-25",
    ]
    assert result["stale_skipped"] == stale_count
    assert result["total"] == valid_count
    assert observed_offsets and set(observed_offsets) == {0}
    assert len(mem0.get_memory_ids) == len(set(mem0.get_memory_ids))
    assert len(mem0.get_memory_ids) <= stale_count + valid_count
    assert 1 < mem0.max_concurrent_gets <= 8


@pytest.mark.asyncio
async def test_query_memories_direct_high_page_validates_from_projection_start(
    db_session,
    monkeypatch,
) -> None:
    _create_project(db_session)
    records: dict[str, Any] = {}
    stale_count = 23
    valid_count = 8
    for index in range(stale_count + valid_count):
        memory_id = f"mem-{index:02d}"
        _index_memory(db_session, memory_id)
        if index < stale_count:
            records[memory_id] = Mem0UpstreamError(
                method="GET",
                path=f"/memories/{memory_id}",
                status_code=404,
                message="missing",
            )
        else:
            records[memory_id] = {"id": memory_id, "memory": "valid"}
    db_session.commit()
    observed_offsets: list[int | None] = []
    original_query = MemoryIndexRepository.query_project_memories

    def capture_offset(self, *args, window_offset=None, **kwargs):
        observed_offsets.append(window_offset)
        return original_query(
            self,
            *args,
            window_offset=window_offset,
            **kwargs,
        )

    monkeypatch.setattr(
        MemoryIndexRepository,
        "query_project_memories",
        capture_offset,
    )
    mem0 = ExplorerMem0Client(records)

    result = await MemoryService(
        session=db_session,
        mem0=mem0,
    ).query_memories(
        project_id="repo-a",
        app_id="app-a",
        query=_explorer_query(page=3, page_size=3),
    )

    assert [item["id"] for item in result["results"]] == [
        "mem-29",
        "mem-30",
    ]
    assert result["total"] == valid_count
    assert result["stale_skipped"] == stale_count
    assert observed_offsets and set(observed_offsets) == {0}
    assert len(mem0.get_memory_ids) == len(set(mem0.get_memory_ids))
    assert len(mem0.get_memory_ids) <= stale_count + valid_count
    assert 1 < mem0.max_concurrent_gets <= 8


@pytest.mark.asyncio
async def test_query_memories_consecutive_pages_remain_gapless_after_compaction(
    db_session,
) -> None:
    _create_project(db_session)
    stale_ids = {"mem-00", "mem-03", "mem-07"}
    records: dict[str, Any] = {}
    for index in range(11):
        memory_id = f"mem-{index:02d}"
        _index_memory(db_session, memory_id)
        if memory_id in stale_ids:
            records[memory_id] = Mem0UpstreamError(
                method="GET",
                path=f"/memories/{memory_id}",
                status_code=404,
                message="missing",
            )
        else:
            records[memory_id] = {"id": memory_id, "memory": "valid"}
    db_session.commit()
    mem0 = ExplorerMem0Client(records)
    service = MemoryService(session=db_session, mem0=mem0)

    first = await service.query_memories(
        project_id="repo-a",
        app_id="app-a",
        query=_explorer_query(page=1, page_size=3),
    )
    first_gets = list(mem0.get_memory_ids)
    second = await service.query_memories(
        project_id="repo-a",
        app_id="app-a",
        query=_explorer_query(page=2, page_size=3),
    )
    second_gets = mem0.get_memory_ids[len(first_gets) :]

    first_ids = [item["id"] for item in first["results"]]
    second_ids = [item["id"] for item in second["results"]]
    assert first_ids == ["mem-01", "mem-02", "mem-04"]
    assert second_ids == ["mem-05", "mem-06", "mem-08"]
    assert len(first_ids + second_ids) == len(set(first_ids + second_ids))
    assert first["total"] == second["total"] == 8
    assert len(first_gets) == len(set(first_gets))
    assert len(second_gets) == len(set(second_gets))
    assert mem0.max_concurrent_gets <= 8


@pytest.mark.asyncio
async def test_query_memories_horizon_failure_persists_cleanup_and_failed_trace(
    db_session,
    monkeypatch,
) -> None:
    _create_project(db_session)
    records: dict[str, Any] = {}
    for index in range(8):
        memory_id = f"mem-{index:02d}"
        _index_memory(db_session, memory_id)
        if index < 5:
            records[memory_id] = Mem0UpstreamError(
                method="GET",
                path=f"/memories/{memory_id}",
                status_code=404,
                message="missing",
            )
        else:
            records[memory_id] = {"id": memory_id, "memory": "valid"}
    db_session.commit()
    monkeypatch.setattr(
        "mem0_sidecar.core.memory_ops.EXPLORER_RECORD_HORIZON",
        5,
    )
    mem0 = ExplorerMem0Client(records)
    result: dict[str, Any] | None = None
    error: MutationConflictError | None = None

    try:
        result = await MemoryService(
            session=db_session,
            mem0=mem0,
        ).query_memories(
            project_id="repo-a",
            app_id="app-a",
            query=_explorer_query(page_size=3),
        )
    except MutationConflictError as exc:
        error = exc

    with Session(db_session.get_bind()) as verification_session:
        stale_count = sum(
            MemoryIndexRepository(verification_session)
            .get_memory(
                project_id="repo-a",
                mem0_memory_id=f"mem-{index:02d}",
                app_id="app-a",
                include_deleted=True,
            )
            .deleted_at
            is not None
            for index in range(5)
        )
        events = EventRepository(verification_session).list_project_events("repo-a")

    assert error is not None and "retry" in str(error).lower(), (
        f"result={result!r} gets={mem0.get_memory_ids!r} stale={stale_count} "
        f"event_statuses={[event.status.value for event in events]!r}"
    )
    assert len(mem0.get_memory_ids) == 5
    assert len(mem0.get_memory_ids) == len(set(mem0.get_memory_ids))
    assert stale_count == 5
    assert len(events) == 1
    assert events[0].status is EventStatus.FAILED
    assert "mem-" not in events[0].error_json


@pytest.mark.asyncio
async def test_query_memories_stale_mark_race_fails_without_loop_or_skip(
    db_session,
    monkeypatch,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "mem-raced")
    _index_memory(db_session, "mem-next")
    db_session.commit()
    mem0 = ExplorerMem0Client(
        {
            "mem-raced": Mem0UpstreamError(
                method="GET",
                path="/memories/mem-raced",
                status_code=404,
                message="missing",
            ),
            "mem-next": {"id": "mem-next", "memory": "must not be skipped"},
        }
    )
    stale_mark_calls = 0

    def refuse_stale_mark(self, **kwargs):
        nonlocal stale_mark_calls
        stale_mark_calls += 1
        return 0

    monkeypatch.setattr(
        MemoryIndexRepository,
        "mark_stale_if_unchanged",
        refuse_stale_mark,
    )

    with pytest.raises(MutationConflictError, match="(?i)retry"):
        await asyncio.wait_for(
            MemoryService(session=db_session, mem0=mem0).query_memories(
                project_id="repo-a",
                app_id="app-a",
                query=_explorer_query(page_size=1),
            ),
            timeout=1,
        )

    assert stale_mark_calls == 1
    assert len(mem0.get_memory_ids) == len(set(mem0.get_memory_ids))
    assert mem0.get_memory_ids == ["mem-raced", "mem-next"]
    raced = MemoryIndexRepository(db_session).get_memory(
        project_id="repo-a",
        mem0_memory_id="mem-raced",
        app_id="app-a",
        include_deleted=True,
    )
    assert raced is not None and raced.deleted_at is None


@pytest.mark.asyncio
async def test_update_fences_projection_mutations_after_upstream_update(
    db_session,
    monkeypatch,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "mem-1")
    db_session.commit()
    operations: list[str] = []

    monkeypatch.setattr(
        ProjectRepository,
        "lock_for_mutation",
        lambda self, project_id: operations.append("project")
        or db_session.get(Project, project_id),
        raising=False,
    )
    original_upsert = MemoryIndexRepository.upsert_memory

    def ordered_upsert(self, **kwargs):
        operations.append("memory")
        return original_upsert(self, **kwargs)

    monkeypatch.setattr(MemoryIndexRepository, "upsert_memory", ordered_upsert)
    monkeypatch.setattr(
        EntityRepository,
        "refresh_affected_memories",
        lambda self, project_id, app_id, memories: operations.append("entity") or [],
    )

    class OrderedUpdateClient(ExplorerMem0Client):
        async def update_memory(self, memory_id, payload):
            operations.append("upstream")
            return await super().update_memory(memory_id, payload)

    mem0 = OrderedUpdateClient(
        {
            "mem-1": {
                "id": "mem-1",
                "memory": "updated",
                "user_id": "alice",
                "app_id": "app-a",
                "metadata": {},
            }
        }
    )

    await MemoryService(session=db_session, mem0=mem0).update_memory(
        project_id="repo-a",
        memory_id="mem-1",
        request_app_id="app-a",
        payload={"text": "updated"},
    )

    upstream_index = operations.index("upstream")
    memory_index = operations.index("memory")
    entity_index = operations.index("entity")
    assert upstream_index < memory_index < entity_index
    assert "project" in operations[upstream_index + 1 : memory_index]


@pytest.mark.asyncio
async def test_reconcile_fences_projection_mutations_after_upstream_observation(
    db_session,
    monkeypatch,
) -> None:
    _create_project(db_session)
    db_session.commit()
    operations: list[str] = []
    monkeypatch.setattr(
        ProjectRepository,
        "lock_for_mutation",
        lambda self, project_id: operations.append("project")
        or db_session.get(Project, project_id),
        raising=False,
    )
    original_upsert = MemoryIndexRepository.upsert_memory

    def ordered_upsert(self, **kwargs):
        operations.append("memory")
        return original_upsert(self, **kwargs)

    monkeypatch.setattr(MemoryIndexRepository, "upsert_memory", ordered_upsert)
    monkeypatch.setattr(
        EntityRepository,
        "refresh_affected_memories",
        lambda self, project_id, app_id, memories: operations.append("entity") or [],
    )
    class OrderedReconcileClient(ExplorerMem0Client):
        async def list_memories(self, params):
            operations.append("upstream")
            return await super().list_memories(params)

    mem0 = OrderedReconcileClient()
    mem0.list_response = {
        "results": [
            {
                "id": "mem-1",
                "memory": "one",
                "app_id": "app-a",
                "metadata": {
                    SIDECAR_PROJECT_ID_METADATA_KEY: "repo-a",
                    SIDECAR_APP_ID_METADATA_KEY: "app-a",
                },
            }
        ],
        "total": 1,
    }

    await MemoryService(session=db_session, mem0=mem0).reconcile_memories(
        project_id="repo-a",
        app_id="app-a",
        adopt_unscoped=False,
        allow_adopt_unscoped=False,
        default_project_id="repo-a",
    )

    upstream_index = operations.index("upstream")
    memory_index = operations.index("memory")
    entity_index = operations.index("entity")
    assert upstream_index < memory_index < entity_index
    assert "project" in operations[upstream_index + 1 : memory_index]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TypeError("bad update"), RuntimeError("bad update")])
async def test_update_memory_preserves_non_value_update_errors_without_stale(
    db_session,
    error: Exception,
) -> None:
    _create_project(db_session)
    projection = _index_memory(db_session, "mem-1")

    class FailingUpdateClient(ExplorerMem0Client):
        async def update_memory(
            self,
            memory_id: str,
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            self.update_calls.append((memory_id, payload))
            raise error

    with pytest.raises(type(error), match="bad update"):
        await MemoryService(
            session=db_session,
            mem0=FailingUpdateClient(),
        ).update_memory(
            project_id="repo-a",
            memory_id="mem-1",
            request_app_id="app-a",
            payload={"text": "after"},
        )

    assert projection.deleted_at is None
    event = EventRepository(db_session).list_project_events("repo-a")[-1]
    assert event.status is EventStatus.FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["update", "refresh"])
async def test_update_memory_wraps_decode_value_errors_at_call_boundaries(
    db_session,
    failure_stage: str,
) -> None:
    _create_project(db_session)
    projection = _index_memory(db_session, "mem-1")
    decode_error = ValueError(f"{failure_stage} response is not JSON")

    class DecodeFailureClient(ExplorerMem0Client):
        async def update_memory(
            self,
            memory_id: str,
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            self.update_calls.append((memory_id, payload))
            if failure_stage == "update":
                raise decode_error
            return {"message": "updated"}

        async def get_memory(self, memory_id: str) -> Any:
            self.get_memory_ids.append(memory_id)
            if failure_stage == "refresh":
                raise decode_error
            return self.records[memory_id]

    mem0 = DecodeFailureClient(
        {"mem-1": {"id": "mem-1", "memory": "before", "metadata": {}}}
    )

    with pytest.raises(MemoryUpstreamProtocolError) as exc_info:
        await MemoryService(session=db_session, mem0=mem0).update_memory(
            project_id="repo-a",
            memory_id="mem-1",
            request_app_id="app-a",
            payload={"text": "after"},
        )

    assert exc_info.value.__cause__ is decode_error
    assert projection.deleted_at is None
    event = EventRepository(db_session).list_project_events("repo-a")[-1]
    assert event.status is EventStatus.FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["update", "refresh"])
async def test_update_memory_preserves_non_404_upstream_http_errors(
    db_session,
    failure_stage: str,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "mem-1")
    upstream_error = Mem0UpstreamError(
        method="PUT" if failure_stage == "update" else "GET",
        path="/memories/mem-1",
        status_code=503,
        message="unavailable",
    )

    class HttpFailureClient(ExplorerMem0Client):
        async def update_memory(
            self,
            memory_id: str,
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            if failure_stage == "update":
                raise upstream_error
            return {"message": "updated"}

        async def get_memory(self, memory_id: str) -> Any:
            if failure_stage == "refresh":
                raise upstream_error
            return self.records[memory_id]

    with pytest.raises(Mem0UpstreamError) as exc_info:
        await MemoryService(
            session=db_session,
            mem0=HttpFailureClient(
                {"mem-1": {"id": "mem-1", "memory": "before"}}
            ),
        ).update_memory(
            project_id="repo-a",
            memory_id="mem-1",
            request_app_id="app-a",
            payload={"text": "after"},
        )

    assert exc_info.value is upstream_error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refresh_response",
    [{"id": "different-id", "memory": "wrong"}, {"results": ["malformed"]}],
)
async def test_update_memory_preserves_refresh_protocol_errors_without_stale(
    db_session,
    refresh_response: Any,
) -> None:
    _create_project(db_session)
    projection = _index_memory(db_session, "mem-1")

    class MalformedRefreshClient(ExplorerMem0Client):
        async def update_memory(
            self,
            memory_id: str,
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            self.update_calls.append((memory_id, payload))
            return {"message": "updated"}

        async def get_memory(self, memory_id: str) -> Any:
            self.get_memory_ids.append(memory_id)
            return refresh_response

    with pytest.raises(MemoryUpstreamProtocolError, match="does not contain"):
        await MemoryService(
            session=db_session,
            mem0=MalformedRefreshClient(),
        ).update_memory(
            project_id="repo-a",
            memory_id="mem-1",
            request_app_id="app-a",
            payload={"text": "after"},
        )

    assert projection.deleted_at is None
    event = EventRepository(db_session).list_project_events("repo-a")[-1]
    assert event.status is EventStatus.FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["update", "refresh"])
async def test_update_memory_maps_only_upstream_404_to_stale_not_found(
    db_session,
    failure_stage: str,
) -> None:
    _create_project(db_session)
    projection = _index_memory(db_session, "mem-1")
    missing = Mem0UpstreamError(
        method="PUT" if failure_stage == "update" else "GET",
        path="/memories/mem-1",
        status_code=404,
        message="missing",
    )

    class MissingDuringUpdateClient(ExplorerMem0Client):
        async def update_memory(
            self,
            memory_id: str,
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            self.update_calls.append((memory_id, payload))
            if failure_stage == "update":
                raise missing
            return {"message": "updated"}

        async def get_memory(self, memory_id: str) -> Any:
            self.get_memory_ids.append(memory_id)
            raise missing

    expected_error = KeyError if failure_stage == "update" else Mem0UpstreamError
    with pytest.raises(expected_error):
        await MemoryService(
            session=db_session,
            mem0=MissingDuringUpdateClient(),
        ).update_memory(
            project_id="repo-a",
            memory_id="mem-1",
            request_app_id="app-a",
            payload={"text": "after"},
        )

    assert (projection.deleted_at is not None) is (failure_stage == "update")
    event = EventRepository(db_session).list_project_events("repo-a")[-1]
    assert event.status is EventStatus.FAILED
    intent = db_session.query(MutationIntent).one()
    assert intent.status == ("FAILED" if failure_stage == "update" else "UNKNOWN")


def _entity_ids(db_session, *, app_id: str = "app-a") -> set[tuple[str, str, int]]:
    return {
        (entity.entity_type, entity.entity_id, entity.memory_count)
        for entity in db_session.query(Entity).filter_by(
            project_id="repo-a",
            app_id=app_id,
        )
    }


@pytest.mark.asyncio
async def test_successful_add_refreshes_all_entity_projections_before_return(
    db_session,
    monkeypatch,
) -> None:
    _create_project(db_session, default_app_id="app-a")
    db_session.commit()
    monkeypatch.setattr(
        EntityRepository,
        "rebuild_project_entities",
        lambda *_args, **_kwargs: pytest.fail(
            "ordinary add must not run a full entity rebuild"
        ),
    )

    await MemoryService(session=db_session, mem0=FakeMem0Client()).add_memory(
        project_id="repo-a",
        payload={
            "text": "hello",
            "app_id": "app-a",
            "user_id": "alice",
            "agent_id": "agent-1",
            "run_id": "run-1",
        },
    )

    assert _entity_ids(db_session) == {
        ("app", "app-a", 1),
        ("user", "alice", 1),
        ("agent", "agent-1", 1),
        ("run", "run-1", 1),
    }


@pytest.mark.asyncio
async def test_successful_update_refreshes_changed_entity_projections_before_commit(
    db_session,
    monkeypatch,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "mem-1")
    EntityRepository(db_session).rebuild_project_entities("repo-a", "app-a")
    db_session.commit()
    monkeypatch.setattr(
        EntityRepository,
        "rebuild_project_entities",
        lambda *_args, **_kwargs: pytest.fail(
            "ordinary update must not run a full entity rebuild"
        ),
    )
    mem0 = ExplorerMem0Client(
        {
            "mem-1": {
                "id": "mem-1",
                "memory": "before",
                "user_id": "bob",
                "agent_id": "agent-2",
                "run_id": "run-2",
                "metadata": {},
            }
        }
    )

    await MemoryService(session=db_session, mem0=mem0).update_memory(
        project_id="repo-a",
        memory_id="mem-1",
        request_app_id="app-a",
        payload={"text": "after"},
    )

    assert _entity_ids(db_session) == {
        ("app", "app-a", 1),
        ("user", "bob", 1),
        ("agent", "agent-2", 1),
        ("run", "run-2", 1),
    }


@pytest.mark.asyncio
async def test_successful_delete_refreshes_entity_projections_before_commit(
    db_session,
    monkeypatch,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "mem-1")
    EntityRepository(db_session).rebuild_project_entities("repo-a", "app-a")
    db_session.commit()
    monkeypatch.setattr(
        EntityRepository,
        "rebuild_project_entities",
        lambda *_args, **_kwargs: pytest.fail(
            "ordinary delete must not run a full entity rebuild"
        ),
    )

    await MemoryService(session=db_session, mem0=FakeMem0Client()).delete_memory(
        project_id="repo-a",
        memory_id="mem-1",
        request_app_id="app-a",
    )

    assert _entity_ids(db_session) == set()


@pytest.mark.asyncio
async def test_successful_reconcile_refreshes_only_affected_entity_scope(
    db_session,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "old")
    EntityRepository(db_session).rebuild_project_entities("repo-a", "app-a")
    db_session.commit()
    mem0 = ExplorerMem0Client()
    mem0.list_response = {
        "results": [
            {
                "id": "new",
                "user_id": "bob",
                "agent_id": "agent-2",
                "run_id": "run-2",
                "metadata": {
                    SIDECAR_PROJECT_ID_METADATA_KEY: "repo-a",
                    SIDECAR_APP_ID_METADATA_KEY: "app-a",
                },
            }
        ]
    }

    await MemoryService(session=db_session, mem0=mem0).reconcile_memories(
        project_id="repo-a",
        app_id="app-a",
        adopt_unscoped=False,
        allow_adopt_unscoped=False,
        default_project_id="repo-a",
    )

    assert _entity_ids(db_session) == {
        ("app", "app-a", 1),
        ("user", "bob", 1),
        ("agent", "agent-2", 1),
        ("run", "run-2", 1),
    }


@pytest.mark.asyncio
async def test_reconcile_app_scope_move_refreshes_old_and_new_entities(
    db_session,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "moved", app_id="app-a")
    EntityRepository(db_session).rebuild_project_entities("repo-a", "app-a")
    db_session.commit()
    mem0 = ExplorerMem0Client()
    mem0.list_response = {
        "results": [
            {
                "id": "moved",
                "user_id": "bob",
                "agent_id": "agent-2",
                "app_id": "app-b",
                "run_id": "run-2",
                "metadata": {
                    SIDECAR_PROJECT_ID_METADATA_KEY: "repo-a",
                    SIDECAR_APP_ID_METADATA_KEY: "app-b",
                },
            }
        ]
    }

    await MemoryService(session=db_session, mem0=mem0).reconcile_memories(
        project_id="repo-a",
        app_id="app-b",
        adopt_unscoped=False,
        allow_adopt_unscoped=False,
        default_project_id="repo-a",
    )

    assert _entity_ids(db_session, app_id="app-a") == set()
    assert _entity_ids(db_session, app_id="app-b") == {
        ("app", "app-b", 1),
        ("user", "bob", 1),
        ("agent", "agent-2", 1),
        ("run", "run-2", 1),
    }


@pytest.mark.asyncio
async def test_failed_memory_mutations_do_not_refresh_prior_entity_projection(
    db_session,
) -> None:
    _create_project(db_session)
    projection = _index_memory(db_session, "mem-1")
    EntityRepository(db_session).rebuild_project_entities("repo-a", "app-a")
    db_session.commit()
    expected = _entity_ids(db_session)

    with pytest.raises(RuntimeError, match="boom"):
        await MemoryService(
            session=db_session,
            mem0=FailingDeleteMem0Client(),
        ).delete_memory(
            project_id="repo-a",
            memory_id="mem-1",
            request_app_id="app-a",
        )

    assert projection.deleted_at is None
    assert _entity_ids(db_session) == expected


@pytest.mark.asyncio
async def test_add_update_delete_and_reconcile_failures_never_run_entity_rebuild(
    db_session,
    monkeypatch,
) -> None:
    _create_project(db_session)
    _index_memory(db_session, "mem-1")
    EntityRepository(db_session).rebuild_project_entities("repo-a", "app-a")
    db_session.commit()
    rebuild_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        EntityRepository,
        "rebuild_project_entities",
        lambda self, project_id, app_id: rebuild_calls.append((project_id, app_id)),
    )

    class FailedMutations(ExplorerMem0Client):
        async def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
            raise Mem0UpstreamError(
                method="POST",
                path="/memories",
                status_code=503,
                message="add failed",
                outcome_unknown=False,
            )

        async def update_memory(
            self,
            memory_id: str,
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            raise Mem0UpstreamError(
                method="PUT",
                path=f"/memories/{memory_id}",
                status_code=503,
                message="update failed",
                outcome_unknown=False,
            )

        async def delete_memory(self, memory_id: str) -> dict[str, Any]:
            raise Mem0UpstreamError(
                method="DELETE",
                path=f"/memories/{memory_id}",
                status_code=503,
                message="delete failed",
                outcome_unknown=False,
            )

    mem0 = FailedMutations()
    mem0.list_response = RuntimeError("list failed")
    service = MemoryService(session=db_session, mem0=mem0)

    with pytest.raises(RuntimeError, match="add failed"):
        await service.add_memory(
            project_id="repo-a",
            payload={"text": "new", "app_id": "app-a"},
        )
    with pytest.raises(RuntimeError, match="update failed"):
        await service.update_memory(
            project_id="repo-a",
            memory_id="mem-1",
            request_app_id="app-a",
            payload={"text": "after"},
        )
    with pytest.raises(RuntimeError, match="delete failed"):
        await service.delete_memory(
            project_id="repo-a",
            memory_id="mem-1",
            request_app_id="app-a",
        )
    with pytest.raises(RuntimeError, match="list failed"):
        await service.reconcile_memories(
            project_id="repo-a",
            app_id="app-a",
            adopt_unscoped=False,
            allow_adopt_unscoped=False,
            default_project_id="repo-a",
        )

    assert rebuild_calls == []
    assert _entity_ids(db_session) == {
        ("app", "app-a", 1),
        ("user", "root", 1),
        ("agent", "codex", 1),
        ("run", "run-1", 1),
    }


@pytest.mark.asyncio
async def test_repeated_add_of_same_memory_keeps_projection_counts_idempotent(
    db_session,
) -> None:
    _create_project(db_session)
    db_session.commit()
    service = MemoryService(session=db_session, mem0=FakeMem0Client())
    payload = {
        "text": "same",
        "app_id": "app-a",
        "user_id": "alice",
        "agent_id": "agent-1",
        "run_id": "run-1",
    }

    await service.add_memory(project_id="repo-a", payload=payload)
    db_session.commit()
    await service.add_memory(project_id="repo-a", payload=payload)

    assert _entity_ids(db_session) == {
        ("app", "app-a", 1),
        ("user", "alice", 1),
        ("agent", "agent-1", 1),
        ("run", "run-1", 1),
    }
