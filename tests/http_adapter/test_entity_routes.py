import socket
import threading
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.core.entities import EntityService
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.mem0_client.client import Mem0UpstreamError
from mem0_sidecar.store.models import Entity, Event, EventStatus, Project
from mem0_sidecar.store.repositories import (
    EntityRepository,
    EventRepository,
    MemoryIndexRepository,
    ProjectRepository,
)


class EntityRouteMem0Client:
    def __init__(self, failures: dict[str, Exception] | None = None) -> None:
        self.failures = dict(failures or {})
        self.deleted_ids: list[str] = []

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        self.deleted_ids.append(memory_id)
        if failure := self.failures.get(memory_id):
            raise failure
        return {"id": memory_id, "message": "deleted"}


def _create_test_app(tmp_path, *, mem0_client: object | None = None):
    return create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.internal",
            default_project_id="repo-a",
        ),
        mem0_client=mem0_client or EntityRouteMem0Client(),
    )


def _seed_memory(
    app,
    memory_id: str,
    *,
    project_id: str = "repo-a",
    app_id: str = "app-a",
    user_id: str | None = "alice",
    agent_id: str | None = "agent-1",
    run_id: str | None = "run-1",
    updated_at: datetime | None = None,
) -> None:
    with app.state.session_factory() as session:
        ProjectRepository(session).upsert_default_project(
            project_id=project_id,
            name=project_id,
            mem0_base_url="http://mem0.internal",
            default_app_id=app_id,
        )
        memory = MemoryIndexRepository(session).upsert_memory(
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
        EntityRepository(session).rebuild_project_entities(project_id, app_id)
        session.commit()


def _track_transactions(monkeypatch) -> tuple[list[Session], list[Session]]:
    commit_calls: list[Session] = []
    rollback_calls: list[Session] = []
    original_commit = Session.commit
    original_rollback = Session.rollback

    def track_commit(session: Session) -> None:
        commit_calls.append(session)
        original_commit(session)

    def track_rollback(session: Session) -> None:
        rollback_calls.append(session)
        original_rollback(session)

    monkeypatch.setattr(Session, "commit", track_commit)
    monkeypatch.setattr(Session, "rollback", track_rollback)
    return commit_calls, rollback_calls


def _raw_http_request(
    port: int,
    target: str,
    *,
    method: str = "GET",
    body: bytes = b"",
) -> tuple[int, bytes]:
    headers = (
        f"{method} {target} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Connection: close\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode("ascii")
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall(headers + body)
        response = bytearray()
        while chunk := connection.recv(65536):
            response.extend(chunk)
    head, _, response_body = bytes(response).partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    return status, response_body


def test_query_entities_returns_scoped_paged_envelope(tmp_path) -> None:
    app = _create_test_app(tmp_path)
    _seed_memory(
        app,
        "older",
        user_id="bob",
        updated_at=datetime(2026, 7, 12, 10, tzinfo=UTC),
    )
    _seed_memory(
        app,
        "newer",
        user_id="alice",
        updated_at=datetime(2026, 7, 13, 10, tzinfo=UTC),
    )

    response = TestClient(app).post(
        "/v1/entities/query",
        json={
            "project_id": "repo-a",
            "app_id": "app-a",
            "entity_type": "USER",
            "page": 1,
            "page_size": 1,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "results": [body["results"][0]],
        "page": 1,
        "page_size": 1,
        "total": 2,
        "has_more": True,
    }
    assert body["results"][0]["entity_id"] == "alice"
    assert body["results"][0]["type"] == "user"


def test_entity_detail_decodes_one_exact_url_segment(tmp_path) -> None:
    app = _create_test_app(tmp_path)
    entity_id = "team/alice+é?%"
    _seed_memory(app, "one", user_id=entity_id)

    response = TestClient(app).get(
        f"/v1/entities/user/{quote(entity_id, safe='')}",
        params={"project_id": "repo-a", "app_id": "app-a"},
    )

    assert response.status_code == 200
    assert response.json()["entity_id"] == entity_id


def test_entity_detail_preserves_literal_percent_encoded_octet(tmp_path) -> None:
    app = _create_test_app(tmp_path)
    entity_id = "alice%2Farchive"
    _seed_memory(app, "literal", user_id=entity_id)
    _seed_memory(app, "slash", user_id="alice/archive")
    encoded_entity_id = quote(entity_id, safe="")

    response = TestClient(app).get(
        f"/v1/entities/user/{encoded_entity_id}",
        params={"project_id": "repo-a", "app_id": "app-a"},
    )

    assert encoded_entity_id == "alice%252Farchive"
    assert response.status_code == 200
    assert response.json()["entity_id"] == entity_id
    assert response.json()["memory_count"] == 1


def test_query_entities_filters_by_exact_scope_and_body_wins_over_query_scope(
    tmp_path,
) -> None:
    app = _create_test_app(tmp_path)
    _seed_memory(app, "visible", project_id="repo-a", app_id="app-a")
    _seed_memory(app, "other-app", project_id="repo-a", app_id="app-b")
    _seed_memory(app, "other-project", project_id="repo-b", app_id="app-b")

    response = TestClient(app).post(
        "/v1/entities/query?project_id=repo-b&app_id=app-b",
        json={
            "project_id": "repo-a",
            "app_id": "app-a",
            "entity_type": "user",
            "filters": [
                {"field": "user_id", "operator": "equals", "value": "alice"}
            ],
            "date_range": {
                "from": "2026-01-01T00:00:00Z",
                "to": "2027-01-01T00:00:00Z",
            },
        },
    )

    assert response.status_code == 200
    assert [item["entity_id"] for item in response.json()["results"]] == [
        "alice"
    ]
    assert response.json()["total"] == 1


def test_query_entities_treats_app_only_as_scope_within_default_project(
    tmp_path,
) -> None:
    app = _create_test_app(tmp_path)
    _seed_memory(app, "visible", project_id="repo-a", app_id="app-a")
    _seed_memory(app, "hidden", project_id="app-a", app_id="app-a")

    response = TestClient(app).post(
        "/v1/entities/query",
        json={"app_id": "app-a", "entity_type": "user"},
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["results"][0]["memory_count"] == 1


def test_query_entities_wrong_app_is_empty_and_detail_is_generic_404(
    tmp_path,
) -> None:
    app = _create_test_app(tmp_path)
    _seed_memory(app, "one")
    client = TestClient(app)

    query = client.post(
        "/v1/entities/query",
        json={
            "project_id": "repo-a",
            "app_id": "app-b",
            "entity_type": "user",
        },
    )
    detail = client.get(
        "/v1/entities/user/alice",
        params={"project_id": "repo-a", "app_id": "app-b"},
    )

    assert query.status_code == 200
    assert query.json() == {
        "results": [],
        "page": 1,
        "page_size": 20,
        "total": 0,
        "has_more": False,
    }
    assert detail.status_code == 404
    assert detail.json() == {"detail": "Entity not found"}


@pytest.mark.parametrize("payload", [[], "USER", 1, True, None])
def test_query_entities_rejects_non_object_json(tmp_path, payload) -> None:
    app = _create_test_app(tmp_path)

    response = TestClient(app).post("/v1/entities/query", json=payload)

    assert response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"unknown": [["x"] * 200]},
        {"sort": "created_at_desc"},
        {"date_range": {"from": None, "to": None, "unknown": "x"}},
        {
            "filters": [
                {
                    "field": "user_id",
                    "operator": "equals",
                    "value": "alice",
                    "unknown": "x",
                }
            ]
        },
        {"filters": [{"field": "session_id", "operator": "equals", "value": "s"}]},
        {"filters": [{"field": "user_id", "operator": "contains", "value": "a"}]},
        {"filters": [{"field": "user_id", "operator": "equals", "value": True}]},
        {
            "filters": [
                {
                    "field": "user_id",
                    "operator": "in",
                    "value": [f"user-{index}" for index in range(101)],
                }
            ]
        },
        {
            "filters": [
                {"field": "user_id", "operator": "equals", "value": "alice"}
            ]
            * 65
        },
        {"entity_type": "SESSION"},
        {"entity_type": True},
        {"match": True},
        {"date_range": "today"},
        {"date_range": {"from": "2026-07-13"}},
        {
            "date_range": {
                "from": "2026-07-14T00:00:00Z",
                "to": "2026-07-13T00:00:00Z",
            }
        },
        {"page": True},
        {"page": 0},
        {"page_size": False},
        {"page_size": 101},
        {"project_id": " repo-a"},
        {"project_id": 7},
        {"app_id": "app a"},
        {"app_id": None},
    ],
)
def test_query_entities_rejects_non_exact_or_invalid_schema(
    tmp_path,
    payload,
) -> None:
    app = _create_test_app(tmp_path)

    response = TestClient(app).post("/v1/entities/query", json=payload)

    assert response.status_code == 422


def test_query_entities_validates_schema_before_unknown_project_lookup(
    tmp_path,
) -> None:
    app = _create_test_app(tmp_path)

    response = TestClient(app).post(
        "/v1/entities/query",
        json={"project_id": "repo-missing", "unknown": [["x"] * 200]},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "unknown query fields: unknown"}
    with app.state.session_factory() as session:
        assert session.get(Project, "repo-missing") is None


def test_query_entities_unknown_project_does_not_bootstrap(tmp_path) -> None:
    app = _create_test_app(tmp_path)

    response = TestClient(app).post(
        "/v1/entities/query",
        json={"project_id": "repo-missing", "app_id": "app-a"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Project not found"}
    with app.state.session_factory() as session:
        assert session.get(Project, "repo-missing") is None


@pytest.mark.parametrize(
    ("entity_type", "entity_id"),
    [
        ("session", "alice"),
        ("user", " leading"),
        ("user", "alice\nadmin"),
        ("user", "e\u0301"),
        ("user", "x" * 257),
    ],
)
def test_entity_detail_rejects_invalid_type_or_nonportable_id(
    tmp_path,
    entity_type: str,
    entity_id: str,
) -> None:
    app = _create_test_app(tmp_path)
    _seed_memory(app, "one")

    response = TestClient(app).get(
        f"/v1/entities/{quote(entity_type, safe='')}/{quote(entity_id, safe='')}",
        params={"project_id": "repo-a", "app_id": "app-a"},
    )

    assert response.status_code in {400, 422}


@pytest.mark.parametrize(
    ("method", "entity_type", "entity_id", "expected_detail"),
    [
        ("GET", "session", "alice", "Unsupported entity type"),
        (
            "GET",
            "user",
            " leading",
            "user_id must be a portable 1-256 character identifier",
        ),
        ("DELETE", "session", "alice", "Unsupported entity type"),
        (
            "DELETE",
            "user",
            " leading",
            "user_id must be a portable 1-256 character identifier",
        ),
    ],
)
def test_entity_item_validation_does_not_reveal_project_existence(
    tmp_path,
    method: str,
    entity_type: str,
    entity_id: str,
    expected_detail: str,
) -> None:
    app = _create_test_app(tmp_path)
    _seed_memory(app, "one")
    client = TestClient(app)
    target = (
        f"/v1/entities/{quote(entity_type, safe='')}/"
        f"{quote(entity_id, safe='')}"
    )

    existing_project = client.request(
        method,
        target,
        params={"project_id": "repo-a", "app_id": "app-a"},
    )
    missing_project = client.request(
        method,
        target,
        params={"project_id": "repo-missing", "app_id": "app-a"},
    )

    assert existing_project.status_code == missing_project.status_code == 422
    assert existing_project.json() == missing_project.json() == {
        "detail": expected_detail
    }
    with app.state.session_factory() as session:
        assert session.get(Project, "repo-missing") is None


def test_entity_detail_unknown_project_and_missing_entity_are_generic_404(
    tmp_path,
) -> None:
    app = _create_test_app(tmp_path)
    client = TestClient(app)

    unknown_project = client.get(
        "/v1/entities/user/alice",
        params={"project_id": "repo-missing", "app_id": "app-a"},
    )
    missing = client.get(
        "/v1/entities/user/missing",
        params={"project_id": "repo-a", "app_id": "repo-a"},
    )

    assert unknown_project.status_code == 404
    assert unknown_project.json() == {"detail": "Entity not found"}
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Entity not found"}
    with app.state.session_factory() as session:
        assert session.get(Project, "repo-missing") is None


def test_entity_router_rejects_malformed_and_traversal_raw_paths(
    tmp_path,
) -> None:
    mem0 = EntityRouteMem0Client()
    app = _create_test_app(tmp_path, mem0_client=mem0)
    _seed_memory(app, "one")
    with app.state.session_factory() as session:
        entity_ids_before = list(session.scalars(select(Entity.id)))
        event_ids_before = list(session.scalars(select(Event.id)))

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    server_thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        daemon=True,
    )
    server_thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started

    scope = "?project_id=repo-a&app_id=app-a"
    try:
        statuses = {
            raw_id: _raw_http_request(
                port,
                f"/v1/entities/user/{raw_id}{scope}",
            )[0]
            for raw_id in (
                "%FF",
                "%C0%AF",
                "%GG",
                "%2E%2E",
                "safe%2F..%2Fescape",
            )
        }
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)
        listener.close()

    assert statuses["%FF"] == 400
    assert statuses["%C0%AF"] == 400
    assert statuses["%GG"] == 404
    assert statuses["%2E%2E"] == 400
    assert statuses["safe%2F..%2Fescape"] == 400
    assert mem0.deleted_ids == []
    with app.state.session_factory() as session:
        assert list(session.scalars(select(Entity.id))) == entity_ids_before
        assert list(session.scalars(select(Event.id))) == event_ids_before


def test_delete_entity_preserves_literal_percent_encoded_octet(tmp_path) -> None:
    mem0 = EntityRouteMem0Client()
    app = _create_test_app(tmp_path, mem0_client=mem0)
    entity_id = "alice%2Farchive"
    _seed_memory(app, "literal", user_id=entity_id)
    _seed_memory(app, "slash", user_id="alice/archive")
    encoded_entity_id = quote(entity_id, safe="")

    response = TestClient(app).delete(
        f"/v1/entities/user/{encoded_entity_id}",
        params={"project_id": "repo-a", "app_id": "app-a"},
    )

    assert encoded_entity_id == "alice%252Farchive"
    assert response.status_code == 200
    assert response.json()["status"] == "SUCCEEDED"
    assert mem0.deleted_ids == ["literal"]
    with app.state.session_factory() as session:
        repository = EntityRepository(session)
        assert repository.list_entity_memory_ids(
            "repo-a", "app-a", "user", entity_id
        ) == []
        assert repository.list_entity_memory_ids(
            "repo-a", "app-a", "user", "alice/archive"
        ) == ["slash"]


@pytest.mark.parametrize(
    ("failures", "expected_status", "expected_deleted", "expected_failed"),
    [
        ({}, "SUCCEEDED", 2, 0),
        ({"two": RuntimeError("no")}, "PARTIAL", 1, 1),
        (
            {"one": RuntimeError("no"), "two": RuntimeError("no")},
            "FAILED",
            0,
            2,
        ),
    ],
)
def test_delete_entity_commits_success_partial_and_failed_results(
    tmp_path,
    monkeypatch,
    failures: dict[str, Exception],
    expected_status: str,
    expected_deleted: int,
    expected_failed: int,
) -> None:
    mem0 = EntityRouteMem0Client(failures)
    app = _create_test_app(tmp_path, mem0_client=mem0)
    _seed_memory(app, "one")
    _seed_memory(app, "two")
    _seed_memory(app, "foreign-app", app_id="app-b")
    commit_calls, rollback_calls = _track_transactions(monkeypatch)

    response = TestClient(app).delete(
        "/v1/entities/user/alice",
        params={"project_id": "repo-a", "app_id": "app-a"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == expected_status
    assert body["requested_count"] == 2
    assert body["deleted_count"] == expected_deleted
    assert body["failed_count"] == expected_failed
    assert len(body["failed"]) == expected_failed
    assert isinstance(body["event_id"], str)
    assert len(commit_calls) == 2
    assert rollback_calls == []
    with app.state.session_factory() as session:
        event = EventRepository(session).get(body["event_id"])
        assert event.status is (
            EventStatus.SUCCEEDED
            if expected_status == "SUCCEEDED"
            else EventStatus.FAILED
        )
        remaining = EntityRepository(session).get_project_entity(
            "repo-a", "app-b", "user", "alice"
        )
        assert remaining.memory_count == 1
        target_ids = EntityRepository(session).list_entity_memory_ids(
            "repo-a", "app-a", "user", "alice"
        )
        assert len(target_ids) == expected_failed


def test_delete_entity_commits_idempotent_upstream_404_convergence(
    tmp_path,
) -> None:
    failure = Mem0UpstreamError(
        method="DELETE",
        path="/memories/one",
        status_code=404,
        message="missing",
    )
    app = _create_test_app(
        tmp_path,
        mem0_client=EntityRouteMem0Client({"one": failure}),
    )
    _seed_memory(app, "one")

    response = TestClient(app).delete(
        "/v1/entities/user/alice",
        params={"project_id": "repo-a", "app_id": "app-a"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "SUCCEEDED"
    assert response.json()["deleted_count"] == 1
    assert response.json()["failed"] == []
    with app.state.session_factory() as session:
        assert EntityRepository(session).list_entity_memory_ids(
            "repo-a", "app-a", "user", "alice"
        ) == []


def test_delete_entity_missing_is_generic_404_without_upstream_access(
    tmp_path,
    monkeypatch,
) -> None:
    mem0 = EntityRouteMem0Client()
    app = _create_test_app(tmp_path, mem0_client=mem0)
    commit_calls, rollback_calls = _track_transactions(monkeypatch)

    response = TestClient(app).delete(
        "/v1/entities/user/missing",
        params={"project_id": "repo-a", "app_id": "repo-a"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Entity not found"}
    assert mem0.deleted_ids == []
    assert commit_calls == []
    assert len(rollback_calls) == 1


def test_delete_entity_rolls_back_only_escaped_errors(
    tmp_path,
    monkeypatch,
) -> None:
    app = _create_test_app(tmp_path)
    _seed_memory(app, "one")
    commit_calls, rollback_calls = _track_transactions(monkeypatch)

    async def escaped_failure(
        self,
        project_id: str,
        app_id: str,
        entity_type: str,
        entity_id: str,
    ) -> dict[str, Any]:
        entity = EntityRepository(self.session).get_project_entity(
            project_id, app_id, entity_type, entity_id
        )
        self.session.delete(entity)
        self.session.flush()
        raise RuntimeError("escaped")

    monkeypatch.setattr(EntityService, "delete_entity", escaped_failure)

    response = TestClient(app, raise_server_exceptions=False).delete(
        "/v1/entities/user/alice",
        params={"project_id": "repo-a", "app_id": "app-a"},
    )

    assert response.status_code == 500
    assert commit_calls == []
    assert len(rollback_calls) == 1
    with app.state.session_factory() as session:
        assert EntityRepository(session).get_project_entity(
            "repo-a", "app-a", "user", "alice"
        ).memory_count == 1


def test_rebuild_entities_uses_resolved_default_app_and_commits(tmp_path) -> None:
    app = _create_test_app(tmp_path)
    _seed_memory(app, "one", user_id="alice")
    with app.state.session_factory() as session:
        session.execute(
            delete(Entity).where(
                Entity.project_id == "repo-a", Entity.app_id == "app-a"
            )
        )
        session.commit()

    response = TestClient(app).post(
        "/v1/projects/repo-a/entities/rebuild",
        json={"project_id": "repo-a"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "entities": 4,
        "project_id": "repo-a",
        "app_id": "app-a",
    }


@pytest.mark.parametrize(
    ("target", "payload"),
    [
        (
            "/v1/projects/repo-a/entities/rebuild",
            {"project_id": "repo-b", "app_id": "app-a"},
        ),
        (
            "/v1/projects/repo-a/entities/rebuild?project_id=repo-b",
            {},
        ),
    ],
)
def test_rebuild_entities_rejects_path_project_mismatch_without_mutation(
    tmp_path,
    target: str,
    payload: dict[str, Any],
) -> None:
    app = _create_test_app(tmp_path)
    _seed_memory(app, "one")
    with app.state.session_factory() as session:
        entity_ids_before = list(session.scalars(select(Entity.id)))

    response = TestClient(app).post(target, json=payload)

    assert response.status_code == 403
    assert response.json() == {"detail": "Project scope mismatch"}
    with app.state.session_factory() as session:
        assert list(session.scalars(select(Entity.id))) == entity_ids_before
        assert session.get(Project, "repo-b") is None


def test_rebuild_entities_never_bootstraps_arbitrary_unknown_project(
    tmp_path,
) -> None:
    app = _create_test_app(tmp_path)

    response = TestClient(app).post(
        "/v1/projects/repo-missing/entities/rebuild",
        json={"project_id": "repo-missing", "app_id": "app-a"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Project not found"}
    with app.state.session_factory() as session:
        assert session.get(Project, "repo-missing") is None


def test_rebuild_entities_may_bootstrap_only_runtime_default_project(
    tmp_path,
) -> None:
    app = _create_test_app(tmp_path)
    with app.state.session_factory() as session:
        session.execute(delete(Project).where(Project.id == "repo-a"))
        session.commit()

    response = TestClient(app).post(
        "/v1/projects/repo-a/entities/rebuild",
        json={"project_id": "repo-a", "app_id": "app-a"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "entities": 0,
        "project_id": "repo-a",
        "app_id": "app-a",
    }
    with app.state.session_factory() as session:
        project = session.get(Project, "repo-a")
        assert project is not None
        assert project.default_app_id == "repo-a"


def test_rebuild_entities_isolates_requested_app(tmp_path) -> None:
    app = _create_test_app(tmp_path)
    _seed_memory(app, "target", app_id="app-a", user_id="alice")
    _seed_memory(app, "foreign", app_id="app-b", user_id="bob")
    with app.state.session_factory() as session:
        foreign_ids_before = list(
            session.scalars(
                select(Entity.id).where(
                    Entity.project_id == "repo-a", Entity.app_id == "app-b"
                )
            )
        )
        session.execute(
            delete(Entity).where(
                Entity.project_id == "repo-a", Entity.app_id == "app-a"
            )
        )
        session.commit()

    response = TestClient(app).post(
        "/v1/projects/repo-a/entities/rebuild",
        json={"project_id": "repo-a", "app_id": "app-a"},
    )

    assert response.status_code == 200
    assert response.json()["entities"] == 4
    with app.state.session_factory() as session:
        assert list(
            session.scalars(
                select(Entity.id).where(
                    Entity.project_id == "repo-a", Entity.app_id == "app-b"
                )
            )
        ) == foreign_ids_before


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"unknown": "x"},
        {"project_id": True},
        {"app_id": "bad app"},
    ],
)
def test_rebuild_entities_rejects_malformed_or_non_exact_body(
    tmp_path,
    payload,
) -> None:
    app = _create_test_app(tmp_path)

    response = TestClient(app).post(
        "/v1/projects/repo-a/entities/rebuild",
        json=payload,
    )

    assert response.status_code == 422
