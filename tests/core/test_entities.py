import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from mem0_sidecar.core.entities import EntityService, parse_entity_query
from mem0_sidecar.mem0_client.client import Mem0UpstreamError
from mem0_sidecar.store.models import EventStatus, MemoryIndex
from mem0_sidecar.store.repositories import (
    EntityRepository,
    EventRepository,
    MemoryIndexRepository,
    ProjectRepository,
)


def _create_project(db_session, project_id: str, app_id: str = "app-a") -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id=project_id,
        name=project_id,
        mem0_base_url="http://mem0.internal:8000",
        default_app_id=app_id,
    )


def _memory(
    db_session,
    memory_id: str,
    *,
    project_id: str = "repo-a",
    app_id: str = "app-a",
    user_id: str | None = "alice",
    agent_id: str | None = "agent-1",
    run_id: str | None = "run-1",
    updated_at: datetime | None = None,
) -> MemoryIndex:
    memory = MemoryIndexRepository(db_session).upsert_memory(
        project_id=project_id,
        mem0_memory_id=memory_id,
        user_id=user_id,
        agent_id=agent_id,
        app_id=app_id,
        run_id=run_id,
        category=None,
        metadata={},
    )
    if updated_at is not None:
        memory.updated_at = updated_at
        db_session.flush()
    return memory


def _rebuild(db_session, project_id: str = "repo-a", app_id: str = "app-a"):
    return EntityRepository(db_session).rebuild_project_entities(project_id, app_id)


class DeleteMem0:
    def __init__(self, failures: dict[str, Exception] | None = None) -> None:
        self.failures = dict(failures or {})
        self.deleted_ids: list[str] = []
        self.delete_all_calls = 0

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        self.deleted_ids.append(memory_id)
        if failure := self.failures.get(memory_id):
            raise failure
        return {"message": "deleted", "id": memory_id}

    async def delete_all_memories(self, payload: dict[str, Any]) -> None:
        self.delete_all_calls += 1
        raise AssertionError(f"broad delete is forbidden: {payload!r}")


def test_parse_entity_query_normalizes_supported_types_and_rejects_boundaries() -> None:
    for value, expected in (
        ("USER", "user"),
        ("RUN", "run"),
        ("AGENT", "agent"),
        ("APP", "app"),
    ):
        assert parse_entity_query({"entity_type": value}).entity_type == expected

    invalid_payloads = (
        {"entity_type": "SESSION"},
        {"entity_type": 1},
        {"page": 0},
        {"page_size": 101},
        {"date_range": {"from": "2026-07-13"}},
        {"date_range": {"from": "2026-07-14T00:00:00Z", "to": "2026-07-13T00:00:00Z"}},
        {"filters": [{"field": "session_id", "operator": "equals", "value": "s"}]},
    )
    for payload in invalid_payloads:
        with pytest.raises(ValueError):
            parse_entity_query(payload)


@pytest.mark.parametrize(
    ("entity_type", "expected_ids"),
    [
        ("USER", ["alice", "bob"]),
        ("RUN", ["run-new", "run-old"]),
        ("AGENT", ["agent-new", "agent-old"]),
        ("APP", ["app-a"]),
    ],
)
def test_query_entities_is_scoped_typed_newest_first_and_serialized(
    db_session,
    entity_type: str,
    expected_ids: list[str],
) -> None:
    _create_project(db_session, "repo-a")
    _create_project(db_session, "repo-b")
    older = datetime(2026, 7, 12, 10, tzinfo=UTC)
    newer = datetime(2026, 7, 13, 10, tzinfo=UTC)
    _memory(
        db_session,
        "old",
        user_id="bob",
        agent_id="agent-old",
        run_id="run-old",
        updated_at=older,
    )
    _memory(
        db_session,
        "new",
        user_id="alice",
        agent_id="agent-new",
        run_id="run-new",
        updated_at=newer,
    )
    _memory(db_session, "other-app", app_id="app-b", user_id="alice")
    _memory(db_session, "other-project", project_id="repo-b", user_id="alice")
    _rebuild(db_session)
    _rebuild(db_session, "repo-a", "app-b")
    _rebuild(db_session, "repo-b", "app-a")

    result = EntityService(session=db_session, mem0=DeleteMem0()).query_entities(
        "repo-a",
        "app-a",
        parse_entity_query({"entity_type": entity_type, "page_size": 10}),
    )

    assert result["total"] == len(expected_ids)
    assert [item["entity_id"] for item in result["results"]] == expected_ids
    assert set(result["results"][0]) == {
        "id",
        "type",
        "entity_id",
        "display_name",
        "memory_count",
        "last_seen_at",
        "updated_at",
    }
    assert all(item["type"] == entity_type.lower() for item in result["results"])


def test_query_entities_supports_all_any_compound_filters_date_and_stable_paging(
    db_session,
) -> None:
    _create_project(db_session, "repo-a")
    instant = datetime(2026, 7, 13, 10, tzinfo=UTC)
    for entity_id in ("alice", "amy", "bob"):
        _memory(
            db_session,
            f"memory-{entity_id}",
            user_id=entity_id,
            updated_at=instant,
        )
    _rebuild(db_session)
    service = EntityService(session=db_session, mem0=DeleteMem0())

    all_result = service.query_entities(
        "repo-a",
        "app-a",
        parse_entity_query(
            {
                "entity_type": "user",
                "match": "all",
                "filters": [
                    {"field": "user_id", "operator": "in", "value": ["alice", "amy"]},
                    {"field": "user_id", "operator": "not_equals", "value": "amy"},
                    {"field": "entity_type", "operator": "equals", "value": "USER"},
                ],
                "date_range": {
                    "from": (instant - timedelta(seconds=1)).isoformat(),
                    "to": (instant + timedelta(seconds=1)).isoformat(),
                },
            }
        ),
    )
    assert [item["entity_id"] for item in all_result["results"]] == ["alice"]

    any_result = service.query_entities(
        "repo-a",
        "app-a",
        parse_entity_query(
            {
                "entity_type": "user",
                "match": "any",
                "filters": [
                    {"field": "user_id", "operator": "equals", "value": "alice"},
                    {"field": "user_id", "operator": "equals", "value": "bob"},
                ],
                "page": 1,
                "page_size": 1,
            }
        ),
    )
    second_page = service.query_entities(
        "repo-a",
        "app-a",
        parse_entity_query(
            {
                "entity_type": "user",
                "match": "any",
                "filters": [
                    {"field": "user_id", "operator": "equals", "value": "alice"},
                    {"field": "user_id", "operator": "equals", "value": "bob"},
                ],
                "page": 2,
                "page_size": 1,
            }
        ),
    )
    assert any_result["total"] == 2
    assert [item["entity_id"] for item in any_result["results"]] == ["alice"]
    assert [item["entity_id"] for item in second_page["results"]] == ["bob"]


def test_query_entities_returns_empty_and_detail_is_strictly_scoped(db_session) -> None:
    _create_project(db_session, "repo-a")
    _memory(db_session, "one", user_id="alice")
    _rebuild(db_session)
    service = EntityService(session=db_session, mem0=DeleteMem0())

    detail = service.get_entity("repo-a", "app-a", "USER", "alice")
    empty = service.query_entities(
        "repo-a",
        "app-b",
        parse_entity_query({"entity_type": "user"}),
    )

    assert detail["entity_id"] == "alice"
    assert detail["memory_count"] == 1
    assert empty == {"results": [], "page": 1, "page_size": 20, "total": 0}
    with pytest.raises(KeyError):
        service.get_entity("repo-a", "app-b", "user", "alice")
    with pytest.raises(ValueError, match="Unsupported entity type"):
        service.get_entity("repo-a", "app-a", "session", "alice")


@pytest.mark.asyncio
async def test_delete_entity_deletes_only_exact_scoped_ids_and_records_success(
    db_session,
    monkeypatch,
) -> None:
    _create_project(db_session, "repo-a")
    _memory(db_session, "target-1", user_id="alice")
    _memory(db_session, "target-2", user_id="alice")
    _memory(db_session, "same-user-other-app", app_id="app-b", user_id="alice")
    _rebuild(db_session)
    _rebuild(db_session, "repo-a", "app-b")
    db_session.commit()
    monkeypatch.setattr(
        "mem0_sidecar.core.entities.get_request_id",
        lambda: "request-entity-delete",
    )
    mem0 = DeleteMem0()

    result = await EntityService(session=db_session, mem0=mem0).delete_entity(
        "repo-a", "app-a", "user", "alice"
    )

    assert result == {
        "status": "SUCCEEDED",
        "requested_count": 2,
        "deleted_count": 2,
        "failed_count": 0,
        "failed": [],
        "event_id": result["event_id"],
    }
    assert set(mem0.deleted_ids) == {"target-1", "target-2"}
    assert mem0.delete_all_calls == 0
    assert EntityRepository(db_session).list_entity_memory_ids(
        "repo-a", "app-a", "user", "alice"
    ) == []
    assert EntityRepository(db_session).list_entity_memory_ids(
        "repo-a", "app-b", "user", "alice"
    ) == ["same-user-other-app"]
    event = EventRepository(db_session).get(result["event_id"])
    assert event.status is EventStatus.SUCCEEDED
    assert event.app_id == "app-a"
    assert event.user_id == "alice"
    assert event.correlation_id == "request-entity-delete"
    assert json.loads(event.request_json) == {
        "app_id": "app-a",
        "entity_id": "alice",
        "entity_type": "user",
        "projected_count": 2,
    }
    assert len(EventRepository(db_session).list_project_events("repo-a")) == 1

    calls_before = list(mem0.deleted_ids)
    with pytest.raises(KeyError):
        await EntityService(session=db_session, mem0=mem0).delete_entity(
            "repo-a", "app-a", "user", "alice"
        )
    assert mem0.deleted_ids == calls_before
    assert len(EventRepository(db_session).list_project_events("repo-a")) == 1


@pytest.mark.asyncio
async def test_delete_entity_partial_failure_keeps_failed_projection_and_safe_event(
    db_session,
) -> None:
    _create_project(db_session, "repo-a")
    _memory(db_session, "ok", user_id="alice")
    _memory(db_session, "fails", user_id="alice")
    _rebuild(db_session)
    db_session.commit()
    secret = "sk_supersecret"
    failure = Mem0UpstreamError(
        method="DELETE",
        path="http://mem0.internal:8000/v1/memories/fails",
        status_code=503,
        message=f"authorization=Bearer {secret}",
        response_text=f"token={secret}",
    )

    result = await EntityService(
        session=db_session,
        mem0=DeleteMem0({"fails": failure}),
    ).delete_entity("repo-a", "app-a", "user", "alice")

    assert result["status"] == "PARTIAL"
    assert result["requested_count"] == 2
    assert result["deleted_count"] == 1
    assert result["failed_count"] == 1
    assert result["failed"][0]["id"] == "fails"
    serialized_result = json.dumps(result)
    assert secret not in serialized_result
    assert "mem0.internal" not in serialized_result
    assert EntityRepository(db_session).list_entity_memory_ids(
        "repo-a", "app-a", "user", "alice"
    ) == ["fails"]
    event = EventRepository(db_session).get(result["event_id"])
    assert event.status is EventStatus.FAILED
    serialized_event = event.request_json + event.response_json + event.error_json
    assert secret not in serialized_event
    assert "mem0.internal" not in serialized_event
    error = json.loads(event.error_json)
    assert error["requested_count"] == 2
    assert error["deleted_count"] == 1
    assert error["failed_count"] == 1
    assert len(EventRepository(db_session).list_project_events("repo-a")) == 1


@pytest.mark.asyncio
async def test_delete_entity_total_failure_and_missing_are_non_destructive(
    db_session,
) -> None:
    _create_project(db_session, "repo-a")
    _memory(db_session, "one", user_id="alice")
    _memory(db_session, "two", user_id="alice")
    _rebuild(db_session)
    db_session.commit()
    mem0 = DeleteMem0(
        {"one": RuntimeError("one failed"), "two": RuntimeError("two failed")}
    )
    service = EntityService(session=db_session, mem0=mem0)

    result = await service.delete_entity("repo-a", "app-a", "USER", "alice")

    assert result["status"] == "FAILED"
    assert result["requested_count"] == 2
    assert result["deleted_count"] == 0
    assert result["failed_count"] == 2
    assert EntityRepository(db_session).get_project_entity(
        "repo-a", "app-a", "user", "alice"
    ).memory_count == 2
    event_count = len(EventRepository(db_session).list_project_events("repo-a"))
    calls_before = list(mem0.deleted_ids)
    with pytest.raises(KeyError):
        await service.delete_entity("repo-a", "app-a", "user", "missing")
    assert mem0.deleted_ids == calls_before
    assert len(EventRepository(db_session).list_project_events("repo-a")) == event_count


@pytest.mark.asyncio
async def test_delete_entity_does_not_commit_caller_transaction(
    db_session,
    monkeypatch,
) -> None:
    _create_project(db_session, "repo-a")
    _memory(db_session, "one", user_id="alice")
    _rebuild(db_session)
    db_session.commit()
    commits = 0
    original_commit = db_session.commit

    def record_commit() -> None:
        nonlocal commits
        commits += 1
        original_commit()

    monkeypatch.setattr(db_session, "commit", record_commit)

    await EntityService(session=db_session, mem0=DeleteMem0()).delete_entity(
        "repo-a", "app-a", "user", "alice"
    )

    assert commits == 0
    assert db_session.in_transaction()


@pytest.mark.asyncio
async def test_delete_entity_locks_project_before_upstream_calls(
    db_session,
    monkeypatch,
) -> None:
    _create_project(db_session, "repo-a")
    _memory(db_session, "one", user_id="alice")
    _rebuild(db_session)
    db_session.commit()
    operations: list[str] = []
    original_scalar = db_session.scalar

    def record_scalar(statement, *args, **kwargs):
        sql = str(statement.compile(dialect=postgresql.dialect())).upper()
        if "FROM PROJECTS" in sql and "FOR UPDATE" in sql:
            operations.append("project_lock")
        return original_scalar(statement, *args, **kwargs)

    class OrderedDeleteMem0(DeleteMem0):
        async def delete_memory(self, memory_id: str) -> dict[str, Any]:
            operations.append("upstream_delete")
            return await super().delete_memory(memory_id)

    monkeypatch.setattr(db_session, "scalar", record_scalar)

    await EntityService(session=db_session, mem0=OrderedDeleteMem0()).delete_entity(
        "repo-a", "app-a", "user", "alice"
    )

    assert operations[0:2] == ["project_lock", "upstream_delete"]
    assert operations[-1] == "project_lock"


@pytest.mark.asyncio
async def test_delete_entity_contains_hostile_error_rendering_per_memory(
    db_session,
) -> None:
    render_calls = 0

    class HostileError(RuntimeError):
        def __str__(self) -> str:
            nonlocal render_calls
            render_calls += 1
            raise RuntimeError("error rendering failed")

    _create_project(db_session, "repo-a")
    _memory(db_session, "one", user_id="alice")
    _memory(db_session, "two", user_id="alice")
    _rebuild(db_session)
    db_session.commit()

    result = await EntityService(
        session=db_session,
        mem0=DeleteMem0({"one": HostileError(), "two": RuntimeError("no")}),
    ).delete_entity("repo-a", "app-a", "user", "alice")

    assert result["status"] == "FAILED"
    assert result["deleted_count"] == 0
    assert result["failed_count"] == 2
    assert {item["id"] for item in result["failed"]} == {"one", "two"}
    assert render_calls == 0


def test_query_uses_bounded_sql_paging_not_unbounded_entity_materialization(
    db_session,
    monkeypatch,
) -> None:
    _create_project(db_session, "repo-a")
    for index in range(30):
        _memory(db_session, f"m-{index:02d}", user_id=f"u-{index:02d}")
    _rebuild(db_session)
    statements: list[str] = []
    original_scalars = db_session.scalars

    def record_scalars(statement, *args, **kwargs):
        statements.append(str(statement))
        return original_scalars(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "scalars", record_scalars)
    result = EntityService(session=db_session, mem0=DeleteMem0()).query_entities(
        "repo-a",
        "app-a",
        parse_entity_query({"entity_type": "user", "page": 2, "page_size": 5}),
    )

    assert len(result["results"]) == 5
    assert any(
        "LIMIT" in statement.upper() and "OFFSET" in statement.upper()
        for statement in statements
    )
