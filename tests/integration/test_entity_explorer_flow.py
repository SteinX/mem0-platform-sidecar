import json
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.mem0_client.client import Mem0UpstreamError
from mem0_sidecar.store.models import Entity, EventStatus, MemoryIndex
from mem0_sidecar.store.repositories import EventRepository


class EntityFlowMem0Client:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.deleted_ids: list[str] = []
        self.delete_failures: dict[str, Exception] = {}
        self._next_id = 1

    async def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        memory_id = f"entity-flow-{self._next_id}"
        self._next_id += 1
        record = {
            "id": memory_id,
            "memory": payload["text"],
            "metadata": dict(payload.get("metadata") or {}),
            "user_id": payload.get("user_id"),
            "agent_id": payload.get("agent_id"),
            "run_id": payload.get("run_id"),
        }
        self.records[memory_id] = record
        return record

    async def get_memory(self, memory_id: str) -> dict[str, Any]:
        return self.records[memory_id]

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        self.deleted_ids.append(memory_id)
        if failure := self.delete_failures.get(memory_id):
            raise failure
        self.records.pop(memory_id, None)
        return {"id": memory_id, "message": "deleted"}


def _create_client(tmp_path, mem0: EntityFlowMem0Client) -> TestClient:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'entity-flow.sqlite3'}",
            mem0_base_url="http://mem0.internal",
            default_project_id="project-a",
        ),
        mem0_client=mem0,
    )
    return TestClient(app)


def _add_memory(
    client: TestClient,
    *,
    project_id: str,
    app_id: str,
    user_id: str,
    agent_id: str | None = None,
    run_id: str | None = None,
) -> str:
    identities = {
        key: value
        for key, value in {"agent_id": agent_id, "run_id": run_id}.items()
        if value is not None
    }
    response = client.post(
        "/v3/memories/add/",
        json={
            "text": f"memory for {project_id}/{app_id}/{user_id}",
            "project_id": project_id,
            "app_id": app_id,
            "user_id": user_id,
            "infer": False,
            **identities,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["memory"]["id"]


def _rebuild(client: TestClient, *, project_id: str, app_id: str) -> dict[str, Any]:
    response = client.post(
        f"/v1/projects/{project_id}/entities/rebuild",
        json={"project_id": project_id, "app_id": app_id},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _query_entities(
    client: TestClient,
    *,
    project_id: str,
    app_id: str,
    entity_type: str,
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    response = client.post(
        "/v1/entities/query",
        json={
            "project_id": project_id,
            "app_id": app_id,
            "entity_type": entity_type,
            "page": page,
            "page_size": page_size,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _query_memories(
    client: TestClient,
    *,
    project_id: str,
    app_id: str,
    field: str,
    value: str,
) -> dict[str, Any]:
    response = client.post(
        "/v1/memories/query",
        json={
            "project_id": project_id,
            "app_id": app_id,
            "match": "all",
            "filters": [
                {"field": field, "operator": "equals", "value": value}
            ],
            "page": 1,
            "page_size": 100,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _entity_by_id(payload: dict[str, Any], entity_id: str) -> dict[str, Any]:
    return next(
        entity for entity in payload["results"] if entity["entity_id"] == entity_id
    )


def _projection_signature(payload: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [
        (
            entity["type"],
            entity["entity_id"],
            entity["display_name"],
            entity["memory_count"],
            entity["last_seen_at"],
        )
        for entity in payload["results"]
    ]


def _exact_scope_projection_signature(
    client: TestClient,
    *,
    project_id: str,
    app_id: str,
) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            entity["id"],
            entity["type"],
            entity["entity_id"],
            entity["display_name"],
            entity["memory_count"],
            entity["last_seen_at"],
            entity["updated_at"],
        )
        for entity_type in ("user", "agent", "app", "run")
        for entity in _query_entities(
            client,
            project_id=project_id,
            app_id=app_id,
            entity_type=entity_type,
        )["results"]
    )


def test_entity_rebuild_query_and_memory_drill_down_are_exactly_scoped(
    tmp_path,
) -> None:
    mem0 = EntityFlowMem0Client()
    client = _create_client(tmp_path, mem0)
    target_ids = {
        _add_memory(
            client,
            project_id="project-a",
            app_id="app-a",
            user_id="shared-user",
            agent_id="agent-a",
            run_id="run-a",
        ),
        _add_memory(
            client,
            project_id="project-a",
            app_id="app-a",
            user_id="shared-user",
            agent_id="agent-b",
            run_id="run-b",
        ),
        _add_memory(
            client,
            project_id="project-a",
            app_id="app-a",
            user_id="other-user",
            agent_id="agent-a",
            run_id="run-c",
        ),
    }
    target_id_by_run = {
        record["run_id"]: memory_id
        for memory_id, record in mem0.records.items()
        if memory_id in target_ids
    }
    foreign_app_id = _add_memory(
        client,
        project_id="project-a",
        app_id="app-b",
        user_id="shared-user",
        agent_id="agent-foreign-app",
        run_id="run-foreign-app",
    )
    foreign_project_id = _add_memory(
        client,
        project_id="project-b",
        app_id="app-a",
        user_id="shared-user",
        agent_id="agent-foreign-project",
        run_id="run-foreign-project",
    )
    foreign_app_projection = _exact_scope_projection_signature(
        client,
        project_id="project-a",
        app_id="app-b",
    )
    foreign_project_projection = _exact_scope_projection_signature(
        client,
        project_id="project-b",
        app_id="app-a",
    )
    assert len(foreign_app_projection) == 4
    assert len(foreign_project_projection) == 4

    first_seen = datetime(2026, 7, 10, 10, tzinfo=UTC)
    second_seen = datetime(2026, 7, 11, 10, tzinfo=UTC)
    third_seen = datetime(2026, 7, 12, 10, tzinfo=UTC)
    latest_seen = datetime(2026, 7, 13, 10, tzinfo=UTC)
    with client.app.state.session_factory() as session:
        for run_id, updated_at in {
            "run-a": first_seen,
            "run-b": second_seen,
            "run-c": third_seen,
        }.items():
            memory = session.scalar(
                select(MemoryIndex).where(
                    MemoryIndex.project_id == "project-a",
                    MemoryIndex.mem0_memory_id == target_id_by_run[run_id],
                )
            )
            assert memory is not None
            memory.updated_at = updated_at
        session.add(
            Entity(
                project_id="project-a",
                app_id="app-a",
                entity_type="user",
                entity_id="obsolete-user",
                memory_count=99,
            )
        )
        session.commit()

    first_rebuild = _rebuild(client, project_id="project-a", app_id="app-a")
    assert first_rebuild == {
        "entities": 8,
        "project_id": "project-a",
        "app_id": "app-a",
    }
    assert _exact_scope_projection_signature(
        client,
        project_id="project-a",
        app_id="app-b",
    ) == foreign_app_projection
    assert _exact_scope_projection_signature(
        client,
        project_id="project-b",
        app_id="app-a",
    ) == foreign_project_projection
    users_before = _query_entities(
        client,
        project_id="project-a",
        app_id="app-a",
        entity_type="user",
    )
    assert {item["entity_id"] for item in users_before["results"]} == {
        "shared-user",
        "other-user",
    }
    assert _entity_by_id(users_before, "shared-user")["memory_count"] == 2
    assert _entity_by_id(users_before, "shared-user")["last_seen_at"] == (
        second_seen.isoformat()
    )
    assert _entity_by_id(users_before, "other-user")["last_seen_at"] == (
        third_seen.isoformat()
    )

    with client.app.state.session_factory() as session:
        memory = session.scalar(
            select(MemoryIndex).where(
                MemoryIndex.project_id == "project-a",
                MemoryIndex.mem0_memory_id == target_id_by_run["run-a"],
            )
        )
        assert memory is not None
        memory.updated_at = latest_seen
        session.commit()

    _rebuild(client, project_id="project-a", app_id="app-a")
    expected_types = {
        "user": {"shared-user": 2, "other-user": 1},
        "agent": {"agent-a": 2, "agent-b": 1},
        "app": {"app-a": 3},
        "run": {"run-a": 1, "run-b": 1, "run-c": 1},
    }
    snapshots: dict[str, list[tuple[Any, ...]]] = {}
    for entity_type, expected_counts in expected_types.items():
        payload = _query_entities(
            client,
            project_id="project-a",
            app_id="app-a",
            entity_type=entity_type,
        )
        assert {
            item["entity_id"]: item["memory_count"] for item in payload["results"]
        } == expected_counts
        snapshots[entity_type] = _projection_signature(payload)

    for entity_type, entity_id in {
        "user": "shared-user",
        "agent": "agent-a",
        "app": "app-a",
        "run": "run-a",
    }.items():
        entity = _entity_by_id(
            _query_entities(
                client,
                project_id="project-a",
                app_id="app-a",
                entity_type=entity_type,
            ),
            entity_id,
        )
        assert entity["last_seen_at"] == latest_seen.isoformat()

    first_page = _query_entities(
        client,
        project_id="project-a",
        app_id="app-a",
        entity_type="user",
        page=1,
        page_size=1,
    )
    second_page = _query_entities(
        client,
        project_id="project-a",
        app_id="app-a",
        entity_type="user",
        page=2,
        page_size=1,
    )
    assert first_page["total"] == second_page["total"] == 2
    assert first_page["has_more"] is True
    assert second_page["has_more"] is False
    assert [item["entity_id"] for item in first_page["results"]] == [
        "shared-user"
    ]
    assert [item["entity_id"] for item in second_page["results"]] == [
        "other-user"
    ]

    expected_drill_down_ids = {
        "user": {
            target_id_by_run["run-a"],
            target_id_by_run["run-b"],
        },
        "agent": {
            target_id_by_run["run-a"],
            target_id_by_run["run-c"],
        },
        "app": target_ids,
        "run": {target_id_by_run["run-a"]},
    }
    for entity_type, entity_id in {
        "user": "shared-user",
        "agent": "agent-a",
        "app": "app-a",
        "run": "run-a",
    }.items():
        detail_response = client.get(
            f"/v1/entities/{entity_type}/{entity_id}",
            params={"project_id": "project-a", "app_id": "app-a"},
        )
        assert detail_response.status_code == 200, detail_response.text
        memories = _query_memories(
            client,
            project_id="project-a",
            app_id="app-a",
            field=f"{entity_type}_id",
            value=entity_id,
        )
        memory_ids = {item["id"] for item in memories["results"]}
        assert memory_ids == expected_drill_down_ids[entity_type]
        assert memories["total"] == detail_response.json()["memory_count"]
        assert memories["stale_skipped"] == 0

    second_rebuild = _rebuild(client, project_id="project-a", app_id="app-a")
    assert second_rebuild == first_rebuild
    for entity_type, expected_signature in snapshots.items():
        assert _projection_signature(
            _query_entities(
                client,
                project_id="project-a",
                app_id="app-a",
                entity_type=entity_type,
            )
        ) == expected_signature
    assert _exact_scope_projection_signature(
        client,
        project_id="project-a",
        app_id="app-b",
    ) == foreign_app_projection
    assert _exact_scope_projection_signature(
        client,
        project_id="project-b",
        app_id="app-a",
    ) == foreign_project_projection

    delete_response = client.delete(
        "/v1/entities/user/shared-user",
        params={"project_id": "project-a", "app_id": "app-a"},
    )
    assert delete_response.status_code == 200, delete_response.text
    assert delete_response.json()["status"] == "SUCCEEDED"
    assert set(mem0.deleted_ids) == expected_drill_down_ids["user"]
    assert foreign_app_id not in mem0.deleted_ids
    assert foreign_project_id not in mem0.deleted_ids
    assert _query_memories(
        client,
        project_id="project-a",
        app_id="app-a",
        field="user_id",
        value="shared-user",
    )["results"] == []
    assert {
        item["id"]
        for item in _query_memories(
            client,
            project_id="project-a",
            app_id="app-b",
            field="user_id",
            value="shared-user",
        )["results"]
    } == {foreign_app_id}
    assert {
        item["id"]
        for item in _query_memories(
            client,
            project_id="project-b",
            app_id="app-a",
            field="user_id",
            value="shared-user",
        )["results"]
    } == {foreign_project_id}
    assert _exact_scope_projection_signature(
        client,
        project_id="project-a",
        app_id="app-b",
    ) == foreign_app_projection
    assert _exact_scope_projection_signature(
        client,
        project_id="project-b",
        app_id="app-a",
    ) == foreign_project_projection


def test_partial_entity_delete_keeps_failures_and_rebuild_cleans_projection(
    tmp_path,
) -> None:
    mem0 = EntityFlowMem0Client()
    client = _create_client(tmp_path, mem0)
    target_ids = {
        _add_memory(
            client,
            project_id="project-a",
            app_id="app-a",
            user_id="partial-user",
        ),
        _add_memory(
            client,
            project_id="project-a",
            app_id="app-a",
            user_id="partial-user",
        ),
    }
    failed_id = sorted(target_ids)[-1]
    foreign_id = _add_memory(
        client,
        project_id="project-a",
        app_id="app-b",
        user_id="partial-user",
    )
    foreign_projection = _exact_scope_projection_signature(
        client,
        project_id="project-a",
        app_id="app-b",
    )
    secret = "sk_entity_flow_secret"
    internal_url = "http://mem0.internal:8000/private"
    mem0.delete_failures[failed_id] = Mem0UpstreamError(
        method="DELETE",
        path=f"{internal_url}/{failed_id}",
        status_code=503,
        message=f"authorization=Bearer {secret}",
        response_text=f"token={secret}",
    )

    partial_response = client.delete(
        "/v1/entities/user/partial-user",
        params={"project_id": "project-a", "app_id": "app-a"},
    )
    assert partial_response.status_code == 200, partial_response.text
    partial = partial_response.json()
    assert partial["status"] == "PARTIAL"
    assert partial["requested_count"] == 2
    assert partial["deleted_count"] == 1
    assert partial["failed_count"] == 1
    expected_failure = {
        "id": failed_id,
        "error": {
            "error_type": "Mem0UpstreamError",
            "message": "Upstream memory deletion failed",
            "upstream_status_code": 503,
        },
    }
    assert partial["failed"] == [expected_failure]
    serialized_partial = json.dumps(partial, sort_keys=True)
    assert secret not in serialized_partial
    assert internal_url not in serialized_partial
    assert "mem0.internal" not in serialized_partial
    assert set(mem0.deleted_ids) == target_ids
    assert foreign_id not in mem0.deleted_ids
    assert _exact_scope_projection_signature(
        client,
        project_id="project-a",
        app_id="app-b",
    ) == foreign_projection

    with client.app.state.session_factory() as session:
        event = EventRepository(session).get(partial["event_id"])
        assert event.operation == "entity.delete"
        assert event.status is EventStatus.FAILED
        assert event.project_id == "project-a"
        assert event.app_id == "app-a"
        assert event.user_id == "partial-user"
        assert event.subject_type == "entity"
        assert event.subject_id == "partial-user"
        persisted_error = json.loads(event.error_json)
        assert persisted_error == {
            "status": "PARTIAL",
            "requested_count": 2,
            "deleted_count": 1,
            "failed_count": 1,
            "failed": [expected_failure],
        }
        serialized_event = "".join(
            (event.request_json, event.response_json, event.error_json)
        )
        assert secret not in serialized_event
        assert internal_url not in serialized_event
        assert "mem0.internal" not in serialized_event

    remaining = _query_memories(
        client,
        project_id="project-a",
        app_id="app-a",
        field="user_id",
        value="partial-user",
    )
    assert [item["id"] for item in remaining["results"]] == [failed_id]
    detail_response = client.get(
        "/v1/entities/user/partial-user",
        params={"project_id": "project-a", "app_id": "app-a"},
    )
    assert detail_response.status_code == 200
    assert detail_response.json()["memory_count"] == 1

    mem0.delete_failures.clear()
    retry_response = client.delete(
        "/v1/entities/user/partial-user",
        params={"project_id": "project-a", "app_id": "app-a"},
    )
    assert retry_response.status_code == 200, retry_response.text
    assert retry_response.json()["status"] == "SUCCEEDED"
    assert mem0.deleted_ids.count(failed_id) == 2
    assert foreign_id not in mem0.deleted_ids
    assert _exact_scope_projection_signature(
        client,
        project_id="project-a",
        app_id="app-b",
    ) == foreign_projection

    with client.app.state.session_factory() as session:
        session.add(
            Entity(
                project_id="project-a",
                app_id="app-a",
                entity_type="user",
                entity_id="obsolete-after-delete",
                memory_count=99,
            )
        )
        session.commit()

    assert _rebuild(client, project_id="project-a", app_id="app-a")["entities"] == 0
    for entity_type in ("user", "agent", "app", "run"):
        assert _query_entities(
            client,
            project_id="project-a",
            app_id="app-a",
            entity_type=entity_type,
        )["results"] == []
    assert _exact_scope_projection_signature(
        client,
        project_id="project-a",
        app_id="app-b",
    ) == foreign_projection
    assert {
        item["id"]
        for item in _query_memories(
            client,
            project_id="project-a",
            app_id="app-b",
            field="user_id",
            value="partial-user",
        )["results"]
    } == {foreign_id}
