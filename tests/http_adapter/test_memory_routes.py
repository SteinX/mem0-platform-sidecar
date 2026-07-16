import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.core.memory_ops import (
    SIDECAR_APP_ID_METADATA_KEY,
    SIDECAR_MUTATION_ID_METADATA_KEY,
    SIDECAR_PROJECT_ID_METADATA_KEY,
    MemoryService,
)
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.mem0_client.client import Mem0UpstreamError
from mem0_sidecar.store.models import Event, EventStatus, MemoryIndex, Project
from mem0_sidecar.store.repositories import (
    EventRepository,
    MemoryIndexRepository,
    MutationIntentRepository,
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
        return {"message": f"Deleted {memory_id}"}


class FailingAddMem0Client(FakeMem0Client):
    async def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.add_payloads.append(payload)
        raise RuntimeError("boom")


class MissingGetMem0Client(FakeMem0Client):
    async def get_memory(self, memory_id: str) -> dict[str, Any]:
        self.get_memory_ids.append(memory_id)
        return {"results": None}


class UpstreamNotFoundMem0Client(FakeMem0Client):
    async def get_memory(self, memory_id: str) -> dict[str, Any]:
        self.get_memory_ids.append(memory_id)
        raise Mem0UpstreamError(
            method="GET",
            path=f"/memories/{memory_id}",
            status_code=404,
            response_text="not found",
            message="not found",
        )

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        self.deleted_ids.append(memory_id)
        raise Mem0UpstreamError(
            method="DELETE",
            path=f"/memories/{memory_id}",
            status_code=404,
            response_text="not found",
            message="not found",
        )


class ExplorerRouteMem0Client(FakeMem0Client):
    def __init__(self, records: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.records = dict(records or {})
        self.get_error: Exception | None = None
        self.update_calls: list[tuple[str, dict[str, Any]]] = []
        self.update_error: Exception | None = None
        self.history_calls: list[str] = []
        self.history_response: Any = {"results": []}
        self.list_calls: list[dict[str, Any]] = []
        self.list_response: Any = {"results": []}

    async def get_memory(self, memory_id: str) -> Any:
        self.get_memory_ids.append(memory_id)
        if self.get_error is not None:
            raise self.get_error
        value = self.records[memory_id]
        if isinstance(value, Exception):
            raise value
        return value

    async def update_memory(
        self,
        memory_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.update_calls.append((memory_id, payload))
        if self.update_error is not None:
            raise self.update_error
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


def _index_route_memory(
    app,
    memory_id: str,
    *,
    project_id: str = "repo-a",
    app_id: str = "app-a",
) -> None:
    with app.state.session_factory() as session:
        MemoryIndexRepository(session).upsert_memory(
            project_id=project_id,
            mem0_memory_id=memory_id,
            user_id="root",
            agent_id="codex",
            app_id=app_id,
            run_id="run-1",
            category="decision",
            metadata={"type": "decision"},
        )
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


def test_memory_routes_round_trip_with_fake_upstream(tmp_path) -> None:
    mem0 = FakeMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    client = TestClient(app)

    add_response = client.post(
        "/v3/memories/add/",
        json={
            "text": "hello",
            "user_id": "root",
            "metadata": {"type": "decision"},
        },
    )
    assert add_response.status_code == 200
    add_body = add_response.json()
    assert add_body["memory"]["id"] == "mem-1"
    assert add_body["event"]["status"] == "SUCCEEDED"
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

    search_response = client.post(
        "/v3/memories/search/",
        json={"query": "hello", "user_id": "root"},
    )
    assert search_response.status_code == 200
    assert search_response.json()["results"][0]["id"] == "mem-1"

    get_response = client.get("/v1/memories/mem-1/")
    assert get_response.status_code == 200
    assert get_response.json()["id"] == "mem-1"
    assert mem0.get_memory_ids == ["mem-1"]

    delete_response = client.delete("/v1/memories/mem-1/")
    assert delete_response.status_code == 200
    assert delete_response.json()["memory"]["message"] == "Deleted mem-1"
    assert mem0.deleted_ids == ["mem-1"]

    events_response = client.get("/v1/events")
    assert events_response.status_code == 200
    assert len(events_response.json()["results"]) >= 2

    event_id = add_body["event"]["id"]
    event_response = client.get(f"/v1/event/{event_id}")
    assert event_response.status_code == 200
    assert event_response.json()["id"] == event_id


def test_failed_mutation_leaves_failed_event_queryable(tmp_path) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=FailingAddMem0Client(),
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/v3/memories/add/",
        json={
            "text": "hello",
            "user_id": "root",
            "metadata": {"type": "decision"},
        },
    )

    assert response.status_code == 500

    events_response = client.get("/v1/events")
    assert events_response.status_code == 200
    events = events_response.json()["results"]
    assert len(events) == 1
    assert events[0]["status"] == EventStatus.FAILED.value
    assert events[0]["operation"] == "memory.add"
    assert events[0]["error"]["error_type"] == "RuntimeError"
    assert events[0]["error"]["message"] == "boom"
    assert events[0]["error"]["request_id"]

    with app.state.session_factory() as session:
        persisted = session.scalars(
            select(Event).where(
                Event.project_id == "repo-a",
                Event.operation == "memory.add",
                Event.status == EventStatus.FAILED,
            )
        ).all()

    assert len(persisted) == 1


def test_event_routes_do_not_leak_other_project_events(tmp_path) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=FakeMem0Client(),
    )

    with app.state.session_factory() as session:
        ProjectRepository(session).upsert_default_project(
            project_id="repo-b",
            name="repo-b",
            mem0_base_url="http://mem0.local",
        )
        event_repo = EventRepository(session)
        visible_event = event_repo.create_event(
            project_id="repo-a",
            operation="memory.add",
            request={"text": "visible"},
            subject_type="memory",
            subject_id="mem-a",
            allow_project_scope=True,
        )
        event_repo.mark_succeeded(visible_event.id, response={"id": "mem-a"})
        hidden_event = event_repo.create_event(
            project_id="repo-b",
            operation="memory.add",
            request={"text": "hidden"},
            subject_type="memory",
            subject_id="mem-b",
            allow_project_scope=True,
        )
        event_repo.mark_succeeded(hidden_event.id, response={"id": "mem-b"})
        session.commit()

    client = TestClient(app)

    events_response = client.get("/v1/events?app_id=repo-a")
    assert events_response.status_code == 200
    event_ids = {event["id"] for event in events_response.json()["results"]}
    assert visible_event.id in event_ids
    assert hidden_event.id not in event_ids

    hidden_detail = client.get(f"/v1/event/{hidden_event.id}?project_id=repo-a")
    assert hidden_detail.status_code == 404


def test_route_scoped_requests_bootstrap_non_default_project_and_normalize_app_id(
    tmp_path,
) -> None:
    mem0 = FakeMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    client = TestClient(app)

    add_response = client.post(
        "/v3/memories/add/",
        json={"text": "hello", "user_id": "root", "project_id": "repo-b"},
    )
    assert add_response.status_code == 200
    assert "app_id" not in mem0.add_payloads[0]
    assert mem0.add_payloads[0]["metadata"][SIDECAR_PROJECT_ID_METADATA_KEY] == "repo-b"
    assert mem0.add_payloads[0]["metadata"][SIDECAR_APP_ID_METADATA_KEY] == "repo-b"
    assert "project_id" not in mem0.add_payloads[0]

    search_response = client.post(
        "/v3/memories/search/",
        json={"query": "hello", "user_id": "root", "project_id": "repo-c"},
    )
    assert search_response.status_code == 200
    assert "app_id" not in mem0.search_payloads[0]
    assert mem0.search_payloads[0]["filters"] == {
        SIDECAR_PROJECT_ID_METADATA_KEY: "repo-c",
        SIDECAR_APP_ID_METADATA_KEY: "repo-c",
    }
    assert "project_id" not in mem0.search_payloads[0]

    events_response = client.get("/v1/events?project_id=repo-b")
    assert events_response.status_code == 200

    with app.state.session_factory() as session:
        project_b = session.scalar(select(Project).where(Project.id == "repo-b"))
        project_c = session.scalar(select(Project).where(Project.id == "repo-c"))

    assert project_b is not None
    assert project_c is None
    assert project_b.mem0_base_url == "http://mem0.local"


def test_route_scoped_add_preserves_explicit_app_id_and_uses_project_scope(
    tmp_path,
) -> None:
    mem0 = FakeMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-default",
        ),
        mem0_client=mem0,
    )
    client = TestClient(app)

    response = client.post(
        "/v3/memories/add/",
        json={
            "text": "hello",
            "user_id": "root",
            "project_id": "repo-a",
            "app_id": "app-x",
        },
    )

    assert response.status_code == 200
    assert "app_id" not in mem0.add_payloads[0]
    assert mem0.add_payloads[0]["metadata"][SIDECAR_PROJECT_ID_METADATA_KEY] == "repo-a"
    assert mem0.add_payloads[0]["metadata"][SIDECAR_APP_ID_METADATA_KEY] == "app-x"
    assert "project_id" not in mem0.add_payloads[0]

    with app.state.session_factory() as session:
        project_a = session.scalar(select(Project).where(Project.id == "repo-a"))
        indexed_memory = session.scalar(
            select(MemoryIndex).where(
                MemoryIndex.project_id == "repo-a",
                MemoryIndex.mem0_memory_id == "mem-1",
            )
        )
        event = session.scalar(
            select(Event).where(
                Event.project_id == "repo-a",
                Event.operation == "memory.add",
            )
        )

    assert project_a is not None
    assert indexed_memory is not None
    assert indexed_memory.app_id == "app-x"
    assert event is not None
    assert json.loads(event.request_json)["metadata"] == {
        SIDECAR_PROJECT_ID_METADATA_KEY: "repo-a",
        SIDECAR_APP_ID_METADATA_KEY: "app-x",
    }


def test_route_scoped_add_preserves_query_app_id_and_bootstraps_default_app_id(
    tmp_path,
) -> None:
    mem0 = FakeMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-default",
        ),
        mem0_client=mem0,
    )
    client = TestClient(app)

    response = client.post(
        "/v3/memories/add/?project_id=repo-a&app_id=app-x",
        json={
            "text": "hello",
            "user_id": "root",
        },
    )

    assert response.status_code == 200
    assert "app_id" not in mem0.add_payloads[0]
    assert mem0.add_payloads[0]["metadata"][SIDECAR_PROJECT_ID_METADATA_KEY] == "repo-a"
    assert mem0.add_payloads[0]["metadata"][SIDECAR_APP_ID_METADATA_KEY] == "app-x"
    assert "project_id" not in mem0.add_payloads[0]

    with app.state.session_factory() as session:
        project_a = session.scalar(select(Project).where(Project.id == "repo-a"))

    assert project_a is not None
    assert project_a.default_app_id == "app-x"


@pytest.mark.parametrize(
    "invalid_app_id",
    [
        "x" * 257,
        "\x00",
        " ",
        "app\n-a",
        " app-a",
        "app-a ",
    ],
)
def test_route_scoped_add_rejects_invalid_app_before_any_mutation(
    tmp_path,
    invalid_app_id: str,
) -> None:
    mem0 = FakeMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-default",
        ),
        mem0_client=mem0,
    )
    with app.state.session_factory() as session:
        ProjectRepository(session).upsert_default_project(
            project_id="repo-a",
            name="Repo A",
            mem0_base_url="http://mem0.local",
            default_app_id="app-good",
        )
        session.commit()

    response = TestClient(app).post(
        "/v3/memories/add/",
        json={
            "project_id": "repo-a",
            "app_id": invalid_app_id,
            "text": "must not be sent",
            "user_id": "root",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"].startswith("app_id must be a portable")
    assert mem0.add_payloads == []
    with app.state.session_factory() as session:
        project = session.get(Project, "repo-a")
        event_count = session.scalar(select(func.count()).select_from(Event))
    assert project is not None
    assert project.default_app_id == "app-good"
    assert event_count == 0


@pytest.mark.parametrize("invalid_key", ["", "contains space", "x" * 129])
def test_route_scoped_add_rejects_invalid_idempotency_key_before_bootstrap(
    tmp_path,
    invalid_key: str,
) -> None:
    mem0 = FakeMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-default",
        ),
        mem0_client=mem0,
    )

    response = TestClient(app).post(
        "/v3/memories/add/",
        headers={"Idempotency-Key": invalid_key},
        json={
            "project_id": "repo-new",
            "app_id": "app-new",
            "text": "must not be sent",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"].startswith(
        "Idempotency-Key must be a visible ASCII value"
    )
    assert mem0.add_payloads == []
    with app.state.session_factory() as session:
        assert session.get(Project, "repo-new") is None
        assert session.scalar(select(func.count()).select_from(Event)) == 0


def test_route_scoped_add_reuses_completed_result_for_same_idempotency_key(
    tmp_path,
) -> None:
    mem0 = FakeMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    client = TestClient(app)
    request = {
        "headers": {"Idempotency-Key": "logical-add-one"},
        "json": {"text": "hello", "app_id": "app-a"},
    }

    first = client.post("/v3/memories/add/", **request)
    second = client.post("/v3/memories/add/", **request)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(mem0.add_payloads) == 1


def test_route_scoped_add_accepts_256_character_app_id(tmp_path) -> None:
    mem0 = FakeMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-default",
        ),
        mem0_client=mem0,
    )
    app_id = "x" * 256

    response = TestClient(app).post(
        "/v3/memories/add/",
        json={
            "project_id": "repo-a",
            "app_id": app_id,
            "text": "accepted",
            "user_id": "root",
        },
    )

    assert response.status_code == 200
    assert len(mem0.add_payloads) == 1
    with app.state.session_factory() as session:
        project = session.get(Project, "repo-a")
        event = session.scalar(select(Event).where(Event.operation == "memory.add"))
    assert project is not None
    assert project.default_app_id == app_id
    assert event is not None
    assert event.app_id == app_id


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("user_id", "u" * 257),
        ("agent_id", "agent\ninvalid"),
        ("run_id", " "),
    ],
)
def test_route_scoped_add_validates_every_entity_before_project_commit(
    tmp_path,
    field_name: str,
    invalid_value: str,
) -> None:
    mem0 = FakeMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-default",
        ),
        mem0_client=mem0,
    )
    with app.state.session_factory() as session:
        ProjectRepository(session).upsert_default_project(
            project_id="repo-a",
            name="Repo A",
            mem0_base_url="http://mem0.local",
            default_app_id="app-old",
        )
        session.commit()
    payload = {
        "project_id": "repo-a",
        "app_id": "app-new",
        "text": "must not be sent",
        "user_id": "user-a",
        field_name: invalid_value,
    }

    response = TestClient(app).post("/v3/memories/add/", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"].startswith(
        f"{field_name} must be a portable"
    )
    assert mem0.add_payloads == []
    with app.state.session_factory() as session:
        project = session.get(Project, "repo-a")
        event_count = session.scalar(select(func.count()).select_from(Event))
    assert project is not None
    assert project.default_app_id == "app-old"
    assert event_count == 0


def test_route_scoped_add_rejects_project_id_over_database_limit(tmp_path) -> None:
    mem0 = FakeMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-default",
        ),
        mem0_client=mem0,
    )
    project_id = "p" * 129

    response = TestClient(app).post(
        "/v3/memories/add/",
        json={
            "project_id": project_id,
            "app_id": "app-a",
            "text": "must not be sent",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"].startswith(
        "project_id must be a portable 1-128"
    )
    assert mem0.add_payloads == []
    with app.state.session_factory() as session:
        assert session.get(Project, project_id) is None
        assert session.scalar(select(func.count()).select_from(Event)) == 0


def test_route_scoped_search_preserves_explicit_app_id_without_bootstrapping_project(
    tmp_path,
) -> None:
    mem0 = FakeMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-default",
        ),
        mem0_client=mem0,
    )
    client = TestClient(app)

    response = client.post(
        "/v3/memories/search/",
        json={
            "query": "hello",
            "user_id": "root",
            "project_id": "repo-a",
            "app_id": "app-x",
        },
    )

    assert response.status_code == 200
    assert "app_id" not in mem0.search_payloads[0]
    assert mem0.search_payloads[0]["filters"] == {
        SIDECAR_PROJECT_ID_METADATA_KEY: "repo-a",
        SIDECAR_APP_ID_METADATA_KEY: "app-x",
    }
    assert "project_id" not in mem0.search_payloads[0]

    with app.state.session_factory() as session:
        project_a = session.scalar(select(Project).where(Project.id == "repo-a"))

    assert project_a is None


def test_route_scoped_search_preserves_query_app_id_without_bootstrapping_project(
    tmp_path,
) -> None:
    mem0 = FakeMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-default",
        ),
        mem0_client=mem0,
    )
    client = TestClient(app)

    response = client.post(
        "/v3/memories/search/?project_id=repo-a&app_id=app-x",
        json={"query": "hello", "user_id": "root"},
    )

    assert response.status_code == 200
    assert "app_id" not in mem0.search_payloads[0]
    assert mem0.search_payloads[0]["filters"] == {
        SIDECAR_PROJECT_ID_METADATA_KEY: "repo-a",
        SIDECAR_APP_ID_METADATA_KEY: "app-x",
    }
    assert "project_id" not in mem0.search_payloads[0]

    with app.state.session_factory() as session:
        project_a = session.scalar(select(Project).where(Project.id == "repo-a"))

    assert project_a is None


def test_route_scoped_add_uses_app_id_as_local_project_fallback(
    tmp_path,
) -> None:
    mem0 = FakeMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-default",
        ),
        mem0_client=mem0,
    )
    client = TestClient(app)

    response = client.post(
        "/v3/memories/add/",
        json={
            "text": "hello",
            "user_id": "root",
            "app_id": "app-x",
        },
    )

    assert response.status_code == 200
    assert "app_id" not in mem0.add_payloads[0]
    assert mem0.add_payloads[0]["metadata"][SIDECAR_PROJECT_ID_METADATA_KEY] == "app-x"
    assert mem0.add_payloads[0]["metadata"][SIDECAR_APP_ID_METADATA_KEY] == "app-x"
    assert "project_id" not in mem0.add_payloads[0]

    with app.state.session_factory() as session:
        project_x = session.scalar(select(Project).where(Project.id == "app-x"))

    assert project_x is not None


def test_event_list_does_not_bootstrap_non_default_project_on_read(tmp_path) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=FakeMem0Client(),
    )
    client = TestClient(app)

    response = client.get("/v1/events?project_id=repo-z")

    assert response.status_code == 200
    assert response.json() == {"results": []}

    with app.state.session_factory() as session:
        project_z = session.scalar(select(Project).where(Project.id == "repo-z"))

    assert project_z is None


def test_get_memory_does_not_bootstrap_unknown_project_on_read(tmp_path) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=FakeMem0Client(),
    )
    client = TestClient(app)

    response = client.get("/v1/memories/mem-1?project_id=repo-z")

    assert response.status_code == 404
    assert response.json() == {"detail": "Memory not found"}

    with app.state.session_factory() as session:
        project_z = session.scalar(select(Project).where(Project.id == "repo-z"))

    assert project_z is None


def test_get_memory_rejects_wrong_query_app_id_without_remote_read(tmp_path) -> None:
    mem0 = FakeMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-default",
        ),
        mem0_client=mem0,
    )
    client = TestClient(app)

    add_response = client.post(
        "/v3/memories/add/",
        json={
            "text": "hello",
            "user_id": "root",
            "project_id": "repo-a",
            "app_id": "app-a",
        },
    )
    assert add_response.status_code == 200

    response = client.get("/v1/memories/mem-1?project_id=repo-a&app_id=app-b")

    assert response.status_code == 404
    assert response.json() == {"detail": "Memory not found"}
    assert mem0.get_memory_ids == []


def test_get_memory_rejects_stale_projection_when_upstream_missing(tmp_path) -> None:
    mem0 = MissingGetMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-default",
        ),
        mem0_client=mem0,
    )
    client = TestClient(app)

    add_response = client.post(
        "/v3/memories/add/",
        json={
            "text": "hello",
            "user_id": "root",
            "project_id": "repo-a",
            "app_id": "app-a",
        },
    )
    assert add_response.status_code == 200

    response = client.get("/v1/memories/mem-1?project_id=repo-a&app_id=app-a")

    assert response.status_code == 404
    assert response.json() == {"detail": "Memory not found"}
    assert mem0.get_memory_ids == ["mem-1"]


def test_get_memory_maps_upstream_404_to_not_found(tmp_path) -> None:
    mem0 = UpstreamNotFoundMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-default",
        ),
        mem0_client=mem0,
    )
    client = TestClient(app)

    add_response = client.post(
        "/v3/memories/add/",
        json={
            "text": "hello",
            "user_id": "root",
            "project_id": "repo-a",
            "app_id": "app-a",
        },
    )
    assert add_response.status_code == 200

    response = client.get("/v1/memories/mem-1?project_id=repo-a&app_id=app-a")

    assert response.status_code == 404
    assert response.json() == {"detail": "Memory not found"}
    assert mem0.get_memory_ids == ["mem-1"]


def test_delete_memory_unknown_project_does_not_bootstrap_or_write_event(
    tmp_path,
) -> None:
    mem0 = FakeMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-default",
        ),
        mem0_client=mem0,
    )
    client = TestClient(app)

    response = client.delete(
        "/v1/memories/mem-1?project_id=repo-a&app_id=app-x"
    )

    assert response.status_code == 404
    assert mem0.deleted_ids == []

    with app.state.session_factory() as session:
        project_a = session.scalar(select(Project).where(Project.id == "repo-a"))
        event = session.scalar(
            select(Event).where(
                Event.project_id == "repo-a",
                Event.operation == "memory.delete",
                Event.status == EventStatus.FAILED,
                Event.subject_id == "mem-1",
            )
        )

    assert project_a is None
    assert event is None


def test_delete_memory_rejects_wrong_query_app_id_without_remote_delete(
    tmp_path,
) -> None:
    mem0 = FakeMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-default",
        ),
        mem0_client=mem0,
    )
    client = TestClient(app)

    add_response = client.post(
        "/v3/memories/add/",
        json={
            "text": "hello",
            "user_id": "root",
            "project_id": "repo-a",
            "app_id": "app-a",
        },
    )
    assert add_response.status_code == 200
    mem0.deleted_ids.clear()

    response = client.delete("/v1/memories/mem-1?project_id=repo-a&app_id=app-b")

    assert response.status_code == 404
    assert response.json() == {"detail": "Memory not found"}
    assert mem0.deleted_ids == []

    with app.state.session_factory() as session:
        event = session.scalar(
            select(Event).where(
                Event.project_id == "repo-a",
                Event.operation == "memory.delete",
                Event.status == EventStatus.FAILED,
                Event.subject_id == "mem-1",
            )
        )

    assert event is not None
    assert json.loads(event.request_json)["app_id"] == "app-b"


def test_delete_memory_maps_upstream_404_to_not_found_and_records_failed_event(
    tmp_path,
) -> None:
    mem0 = UpstreamNotFoundMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-default",
        ),
        mem0_client=mem0,
    )
    client = TestClient(app)

    add_response = client.post(
        "/v3/memories/add/",
        json={
            "text": "hello",
            "user_id": "root",
            "project_id": "repo-a",
            "app_id": "app-a",
        },
    )
    assert add_response.status_code == 200
    mem0.deleted_ids.clear()

    response = client.delete("/v1/memories/mem-1?project_id=repo-a&app_id=app-a")

    assert response.status_code == 404
    assert response.json() == {"detail": "Memory not found"}
    assert mem0.deleted_ids == ["mem-1"]

    with app.state.session_factory() as session:
        event = session.scalar(
            select(Event).where(
                Event.project_id == "repo-a",
                Event.operation == "memory.delete",
                Event.status == EventStatus.FAILED,
                Event.subject_id == "mem-1",
            )
        )

    assert event is not None
    error = json.loads(event.error_json)
    assert error["upstream_status_code"] == 404
    assert error["upstream_method"] == "DELETE"


def test_route_scoped_requests_allow_different_project_and_app_scope(tmp_path) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=FakeMem0Client(),
    )
    client = TestClient(app)

    add_response = client.post(
        "/v3/memories/add/",
        json={
            "text": "hello",
            "project_id": "repo-a",
            "app_id": "repo-b",
        },
    )
    assert add_response.status_code == 200
    assert "project_id" not in add_response.json()["memory"]

    events_response = client.get("/v1/events?project_id=repo-a&app_id=repo-b")
    assert events_response.status_code == 200

    event_response = client.get(
        "/v1/event/does-not-matter?project_id=repo-a&app_id=repo-b"
    )
    assert event_response.status_code == 404


def test_query_memories_uses_body_scope_and_returns_public_envelope(
    tmp_path,
    monkeypatch,
) -> None:
    expected_memory = {
        "id": "mem-1",
        "memory": "hello",
        "metadata": {"type": "decision"},
        "categories": ["decision"],
        "user_id": "root",
        "agent_id": "codex",
        "app_id": "app-a",
        "run_id": "run-1",
        "created_at": "2026-07-01T10:00:00Z",
        "updated_at": "2026-07-02T10:00:00Z",
        "expiration_date": None,
    }
    transaction_states: list[bool] = []
    original_query_memories = MemoryService.query_memories

    async def query_memories_with_transaction_probe(self, **kwargs):
        transaction_states.append(self.session.in_transaction())
        return await original_query_memories(self, **kwargs)

    monkeypatch.setattr(
        MemoryService,
        "query_memories",
        query_memories_with_transaction_probe,
    )
    mem0 = ExplorerRouteMem0Client({"mem-1": dict(expected_memory)})
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    _index_route_memory(app, "mem-1")

    response = TestClient(app).post(
        "/v1/memories/query?project_id=repo-b&app_id=app-b",
        json={"project_id": "repo-a", "app_id": "app-a"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "results": [expected_memory],
        "page": 1,
        "page_size": 20,
        "total": 1,
        "has_more": False,
        "stale_skipped": 0,
    }
    assert mem0.get_memory_ids == ["mem-1"]
    assert transaction_states == [False]


def test_query_memories_uses_existing_project_default_app_when_omitted(
    tmp_path,
) -> None:
    expected_memory = {
        "id": "mem-app-x",
        "memory": "hello",
        "metadata": {},
        "categories": [],
        "user_id": "root",
        "agent_id": "codex",
        "app_id": "app-x",
        "run_id": "run-1",
        "created_at": "2026-07-01T10:00:00Z",
        "updated_at": "2026-07-02T10:00:00Z",
        "expiration_date": None,
    }
    mem0 = ExplorerRouteMem0Client({"mem-app-x": dict(expected_memory)})
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    with app.state.session_factory() as session:
        project = session.get(Project, "repo-a")
        assert project is not None
        project.default_app_id = "app-x"
        session.commit()
    _index_route_memory(app, "mem-app-x", app_id="app-x")

    response = TestClient(app).post(
        "/v1/memories/query",
        json={"project_id": "repo-a"},
    )

    assert response.status_code == 200
    assert response.json()["results"] == [expected_memory]
    assert mem0.get_memory_ids == ["mem-app-x"]


def test_query_memories_unknown_project_without_app_is_404_and_not_bootstrapped(
    tmp_path,
) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=ExplorerRouteMem0Client(),
    )

    response = TestClient(app).post(
        "/v1/memories/query",
        json={"project_id": "repo-z"},
    )

    assert response.status_code == 404
    with app.state.session_factory() as session:
        assert session.get(Project, "repo-z") is None


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        (
            "POST",
            "/v3/memories/add?project_id=repo-a&app_id=app-a",
            {"text": "blocked"},
        ),
        (
            "PATCH",
            "/v1/memories/mem-1?project_id=repo-a&app_id=app-a",
            {"text": "after"},
        ),
        (
            "DELETE",
            "/v1/memories/mem-1?project_id=repo-a&app_id=app-a",
            None,
        ),
        (
            "DELETE",
            "/v1/entities/user/alice?project_id=repo-a&app_id=app-a",
            None,
        ),
    ],
)
def test_unresolved_mutation_scope_maps_to_conflict_without_upstream_write(
    tmp_path,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    mem0 = ExplorerRouteMem0Client(
        {"mem-1": {"id": "mem-1", "memory": "before", "metadata": {}}}
    )
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    _index_route_memory(app, "mem-1")
    with app.state.session_factory() as session:
        event = EventRepository(session).create_event(
            project_id="repo-a",
            app_id="app-a",
            operation="memory.add",
            request={},
            subject_type="memory",
        )
        intent = MutationIntentRepository(session).create(
            project_id="repo-a",
            app_id="app-a",
            event_id=event.id,
            operation="memory.add",
            payload={},
        )
        intent.status = "UNKNOWN"
        intent.lease_expires_at = None
        session.commit()

    response = TestClient(app, raise_server_exceptions=False).request(
        method,
        path,
        json=body,
    )

    assert response.status_code == 409
    assert response.json()["detail"].endswith("remains unresolved")
    assert mem0.add_payloads == []
    assert mem0.update_calls == []
    assert mem0.deleted_ids == []


@pytest.mark.parametrize("method", ["get", "patch", "history", "delete"])
def test_memory_routes_decode_proxy_transport_id_once(tmp_path, method: str) -> None:
    memory_id = "memory/one"
    mem0 = ExplorerRouteMem0Client(
        {
            memory_id: {
                "id": memory_id,
                "memory": "before",
                "metadata": {},
            }
        }
    )
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / f'{method}.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    _index_route_memory(app, memory_id)
    client = TestClient(app)
    path = "/v1/memories/memory%252Fone"
    params = {"project_id": "repo-a", "app_id": "app-a"}

    if method == "get":
        response = client.get(path, params=params)
        observed = mem0.get_memory_ids
    elif method == "patch":
        response = client.patch(path, params=params, json={"text": "after"})
        observed = [memory for memory, _ in mem0.update_calls]
    elif method == "history":
        response = client.get(f"{path}/history", params=params)
        observed = mem0.history_calls
    else:
        response = client.delete(path, params=params)
        observed = mem0.deleted_ids

    assert response.status_code == 200
    assert observed == [memory_id]


def test_memory_route_once_encoded_slash_does_not_match_item_route(tmp_path) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=ExplorerRouteMem0Client(),
    )

    response = TestClient(app).get("/v1/memories/memory%2Fone")

    assert response.status_code == 404


@pytest.mark.parametrize(
    "encoded_id",
    [
        "safe%252F..%252Fhealth",
        "%252e%252e%255chealth",
        "%2571uery",
        "safe%2500name",
    ],
)
def test_memory_routes_reject_malicious_encoded_ids(
    tmp_path,
    encoded_id: str,
) -> None:
    mem0 = ExplorerRouteMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )

    response = TestClient(app).get(f"/v1/memories/{encoded_id}")

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid memory ID"}
    assert mem0.get_memory_ids == []


def test_dashboard_proxy_round_trips_encoded_id_through_real_sidecar(
    tmp_path,
) -> None:
    memory_id = "memory/one"
    mem0 = ExplorerRouteMem0Client(
        {memory_id: {"id": memory_id, "memory": "hello", "metadata": {}}}
    )
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    _index_route_memory(app, memory_id)
    with app.state.session_factory() as session:
        project = session.get(Project, "repo-a")
        assert project is not None
        project.default_app_id = "app-a"
        session.commit()
    mem0.history_response = {"history": [{"event": "UPDATE"}]}

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", lifespan="off")
    )
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

    root = Path(__file__).resolve().parents[2]
    overlay = root / "integrations/mem0-dashboard-overlay"
    upstream = root.parents[2] / "upstream/server/dashboard"
    dashboard = tmp_path / "dashboard"
    shutil.copytree(
        upstream,
        dashboard,
        ignore=shutil.ignore_patterns("node_modules", ".next"),
        symlinks=True,
    )
    (dashboard / "node_modules").symlink_to(
        upstream / "node_modules",
        target_is_directory=True,
    )
    apply_result = subprocess.run(
        [
            sys.executable,
            str(overlay / "scripts/apply-dashboard-overlay"),
            str(dashboard),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert apply_result.returncode == 0, apply_result.stderr

    try:
        result = subprocess.run(
            [
                "node",
                str(overlay / "scripts/test-sidecar-proxy.cjs"),
                str(dashboard),
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
            env={
                **os.environ,
                "SIDECAR_PROXY_INTEGRATION_URL": f"http://127.0.0.1:{port}",
            },
        )
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)
        listener.close()

    assert result.returncode == 0, result.stderr
    assert "sidecar proxy integration: 3 contracts passed" in result.stdout
    assert mem0.get_memory_ids == [memory_id, memory_id]
    assert mem0.history_calls == [memory_id]


def test_memory_router_rejects_invalid_raw_paths_without_lossy_aliases(
    tmp_path,
) -> None:
    replacement = "\ufffd"
    double_replacement = replacement * 2
    literal_percent = "%GG"
    mem0 = ExplorerRouteMem0Client(
        {
            replacement: {"id": replacement, "memory": "replacement"},
            double_replacement: {
                "id": double_replacement,
                "memory": "double replacement",
            },
            literal_percent: {"id": literal_percent, "memory": "percent"},
        }
    )
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    for memory_id in (replacement, double_replacement, literal_percent):
        _index_route_memory(app, memory_id)
    with app.state.session_factory() as session:
        project = session.get(Project, "repo-a")
        assert project is not None
        project.default_app_id = "app-a"
        project_ids_before = list(session.scalars(select(Project.id)))
        memory_ids_before = list(
            session.scalars(select(MemoryIndex.mem0_memory_id))
        )
        event_ids_before = list(session.scalars(select(Event.id)))
        session.commit()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", lifespan="off")
    )
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
        for raw_id in ("%FF", "%C0%AF", "%GG"):
            status, _ = _raw_http_request(
                port,
                f"/v1/memories/{raw_id}{scope}",
            )
            assert status == 404, raw_id

        reconcile_body = json.dumps(
            {"project_id": replacement, "app_id": "app-a"}
        ).encode()
        reconcile_status, _ = _raw_http_request(
            port,
            "/v1/projects/%FF/memories/reconcile",
            method="POST",
            body=reconcile_body,
        )
        assert reconcile_status == 404
        assert mem0.get_memory_ids == []
        assert mem0.list_calls == []

        replacement_status, _ = _raw_http_request(
            port,
            f"/v1/memories/%EF%BF%BD{scope}",
        )
        literal_status, _ = _raw_http_request(
            port,
            f"/v1/memories/%25GG{scope}",
        )
        proxy_literal_status, _ = _raw_http_request(
            port,
            f"/v1/memories/%2525GG{scope}",
        )
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)
        listener.close()

    assert replacement_status == 200
    assert literal_status == 200
    assert proxy_literal_status == 200
    assert mem0.get_memory_ids == [replacement, literal_percent, literal_percent]
    with app.state.session_factory() as session:
        assert list(session.scalars(select(Project.id))) == project_ids_before
        assert list(session.scalars(select(MemoryIndex.mem0_memory_id))) == (
            memory_ids_before
        )
        assert list(session.scalars(select(Event.id))) == event_ids_before


def test_memory_item_routes_preserve_distinct_opaque_ids_for_every_action(
    tmp_path,
) -> None:
    opaque_ids = {
        "a/b": "a%252Fb",
        "a%b": "a%2525b",
        "a%2Fb": "a%25252Fb",
    }
    mem0 = ExplorerRouteMem0Client(
        {
            memory_id: {
                "id": memory_id,
                "memory": f"memory {memory_id}",
                "app_id": "app-a",
                "metadata": {},
            }
            for memory_id in opaque_ids
        }
    )
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'opaque-memory-ids.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    for memory_id in opaque_ids:
        _index_route_memory(app, memory_id)
    with app.state.session_factory() as session:
        project = session.get(Project, "repo-a")
        assert project is not None
        project.default_app_id = "app-a"
        session.commit()

    with TestClient(app) as client:
        for memory_id, transport_id in opaque_ids.items():
            scope = "?project_id=repo-a&app_id=app-a"
            detail = client.get(f"/v1/memories/{transport_id}{scope}")
            history = client.get(
                f"/v1/memories/{transport_id}/history{scope}"
            )
            update = client.patch(
                f"/v1/memories/{transport_id}{scope}",
                json={"text": f"updated {memory_id}"},
            )
            delete = client.delete(f"/v1/memories/{transport_id}{scope}")

            assert detail.status_code == 200, memory_id
            assert detail.json()["id"] == memory_id
            assert history.status_code == 200, memory_id
            assert update.status_code == 200, memory_id
            assert update.json()["memory"]["id"] == memory_id
            assert delete.status_code == 200, memory_id

    assert mem0.get_memory_ids == [
        memory_id
        for memory_id in opaque_ids
        for _action in ("detail", "update-refresh")
    ]
    assert mem0.history_calls == list(opaque_ids)
    assert [memory_id for memory_id, _payload in mem0.update_calls] == list(
        opaque_ids
    )
    assert mem0.deleted_ids == list(opaque_ids)


def test_query_memories_commits_stale_cleanup(tmp_path) -> None:
    mem0 = ExplorerRouteMem0Client({"mem-stale": ValueError("malformed")})
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    _index_route_memory(app, "mem-stale")

    response = TestClient(app).post(
        "/v1/memories/query",
        json={"project_id": "repo-a", "app_id": "app-a"},
    )

    assert response.status_code == 200
    assert response.json()["stale_skipped"] == 1
    with app.state.session_factory() as session:
        stale = MemoryIndexRepository(session).get_memory(
            project_id="repo-a",
            mem0_memory_id="mem-stale",
            include_deleted=True,
        )
    assert stale is not None and stale.deleted_at is not None


def test_query_memories_never_returns_empty_reachable_page(tmp_path) -> None:
    stale_count = 23
    valid_count = 5
    records: dict[str, Any] = {}
    for index in range(stale_count + valid_count):
        memory_id = f"mem-{index:02d}"
        records[memory_id] = (
            ValueError("malformed")
            if index < stale_count
            else {"id": memory_id, "memory": "valid"}
        )
    mem0 = ExplorerRouteMem0Client(records)
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'reachable-page.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    for memory_id in records:
        _index_route_memory(app, memory_id)

    response = TestClient(app).post(
        "/v1/memories/query",
        json={
            "project_id": "repo-a",
            "app_id": "app-a",
            "page": 1,
            "page_size": 3,
            "sort": "created_at_asc",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload["results"]] == [
        "mem-23",
        "mem-24",
        "mem-25",
    ]
    assert payload == {
        **payload,
        "page": 1,
        "page_size": 3,
        "total": valid_count,
        "has_more": True,
        "stale_skipped": stale_count,
    }
    assert len(mem0.get_memory_ids) == len(set(mem0.get_memory_ids))


def test_query_memories_unknown_project_is_404_without_bootstrap(tmp_path) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=ExplorerRouteMem0Client(),
    )

    response = TestClient(app).post(
        "/v1/memories/query",
        json={"project_id": "repo-z", "app_id": "app-z"},
    )

    assert response.status_code == 404
    with app.state.session_factory() as session:
        project = session.scalar(select(Project).where(Project.id == "repo-z"))
    assert project is None


def test_query_memories_maps_invalid_filters_to_422(tmp_path) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=ExplorerRouteMem0Client(),
    )

    response = TestClient(app).post(
        "/v1/memories/query",
        json={
            "project_id": "repo-a",
            "app_id": "app-a",
            "filters": [
                {"field": "secret", "operator": "equals", "value": "x"}
            ],
        },
    )

    assert response.status_code == 422
    assert "not allowed" in response.json()["detail"]


def test_query_memories_maps_metadata_scan_limit_to_422(tmp_path) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=ExplorerRouteMem0Client(),
    )
    with app.state.session_factory() as session:
        session.execute(
            insert(MemoryIndex),
            [
                {
                    "project_id": "repo-a",
                    "mem0_memory_id": f"mem-{index:04d}",
                    "app_id": "app-a",
                    "metadata_projection_json": '{"source": "codex"}',
                }
                for index in range(5001)
            ],
        )
        session.commit()

    response = TestClient(app).post(
        "/v1/memories/query",
        json={
            "project_id": "repo-a",
            "app_id": "app-a",
            "filters": [
                {
                    "field": "metadata",
                    "operator": "contains",
                    "value": {"key": "source", "value": "codex"},
                }
            ],
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "metadata filter scan exceeds 5000 records"
    }


def test_patch_memory_requires_json_object(tmp_path) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=ExplorerRouteMem0Client(),
    )

    response = TestClient(app).patch(
        "/v1/memories/mem-1?project_id=repo-a&app_id=app-a",
        json=["not", "an", "object"],
    )

    assert response.status_code == 422


def test_patch_memory_commits_success(tmp_path, monkeypatch) -> None:
    record = {
        "id": "mem-1",
        "memory": "before",
        "metadata": {"type": "decision"},
    }
    mem0 = ExplorerRouteMem0Client({"mem-1": record})
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    _index_route_memory(app, "mem-1")
    commit_calls, rollback_calls = _track_transactions(monkeypatch)

    response = TestClient(app).patch(
        "/v1/memories/mem-1?project_id=repo-a&app_id=app-a",
        json={"text": "after"},
    )

    assert response.status_code == 200
    assert response.json()["memory"]["memory"] == "after"
    assert mem0.update_calls == [("mem-1", {"text": "after"})]
    assert len(commit_calls) == 2
    assert rollback_calls == []
    with app.state.session_factory() as session:
        event = session.scalar(
            select(Event).where(
                Event.project_id == "repo-a",
                Event.operation == "memory.update",
                Event.status == EventStatus.SUCCEEDED,
            )
        )
    assert event is not None


def test_patch_memory_rolls_back_and_maps_validation_to_422(
    tmp_path,
    monkeypatch,
) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=ExplorerRouteMem0Client(),
    )
    _index_route_memory(app, "mem-1")
    commit_calls, rollback_calls = _track_transactions(monkeypatch)

    response = TestClient(app).patch(
        "/v1/memories/mem-1?project_id=repo-a&app_id=app-a",
        json={"unsupported": True},
    )

    assert response.status_code == 422
    assert "Unsupported memory update fields" in response.json()["detail"]
    assert commit_calls == []
    assert len(rollback_calls) == 1
    with app.state.session_factory() as session:
        events = session.scalars(
            select(Event).where(Event.project_id == "repo-a")
        ).all()
        projection = MemoryIndexRepository(session).get_memory(
            project_id="repo-a",
            mem0_memory_id="mem-1",
            app_id="app-a",
        )
    assert events == []
    assert projection is not None and projection.deleted_at is None


def test_patch_memory_wrong_app_is_404_without_upstream_access(tmp_path) -> None:
    mem0 = ExplorerRouteMem0Client(
        {"mem-1": {"id": "mem-1", "memory": "before"}}
    )
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    _index_route_memory(app, "mem-1")

    response = TestClient(app).patch(
        "/v1/memories/mem-1?project_id=repo-a&app_id=app-b",
        json={"text": "after"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Memory not found"}
    assert mem0.update_calls == []
    assert mem0.get_memory_ids == []


def test_patch_memory_rolls_back_unexpected_failure(tmp_path, monkeypatch) -> None:
    mem0 = ExplorerRouteMem0Client(
        {"mem-1": {"id": "mem-1", "memory": "before"}}
    )
    mem0.update_error = RuntimeError("boom")
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    _index_route_memory(app, "mem-1")
    commit_calls, rollback_calls = _track_transactions(monkeypatch)

    response = TestClient(app, raise_server_exceptions=False).patch(
        "/v1/memories/mem-1?project_id=repo-a&app_id=app-a",
        json={"text": "after"},
    )

    assert response.status_code == 500
    assert len(commit_calls) == 2
    assert len(rollback_calls) == 2
    with app.state.session_factory() as session:
        event = session.scalar(
            select(Event).where(
                Event.project_id == "repo-a",
                Event.operation == "memory.update",
                Event.status == EventStatus.FAILED,
            )
        )
        projection = MemoryIndexRepository(session).get_memory(
            project_id="repo-a",
            mem0_memory_id="mem-1",
            app_id="app-a",
        )
    assert event is not None
    assert projection is not None and projection.deleted_at is None


def test_patch_memory_persists_failed_event_and_stale_marker_on_upstream_404(
    tmp_path,
    monkeypatch,
) -> None:
    mem0 = ExplorerRouteMem0Client(
        {"mem-1": {"id": "mem-1", "memory": "before"}}
    )
    mem0.update_error = Mem0UpstreamError(
        method="PUT",
        path="/memories/mem-1",
        status_code=404,
        message="missing",
    )
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    _index_route_memory(app, "mem-1")
    commit_calls, rollback_calls = _track_transactions(monkeypatch)

    response = TestClient(app).patch(
        "/v1/memories/mem-1?project_id=repo-a&app_id=app-a",
        json={"text": "after"},
    )

    assert response.status_code == 404
    assert len(commit_calls) == 2
    assert len(rollback_calls) == 2
    with app.state.session_factory() as session:
        event = session.scalar(
            select(Event).where(
                Event.project_id == "repo-a",
                Event.operation == "memory.update",
                Event.status == EventStatus.FAILED,
            )
        )
        stale = MemoryIndexRepository(session).get_memory(
            project_id="repo-a",
            mem0_memory_id="mem-1",
            include_deleted=True,
        )
    assert event is not None
    assert stale is not None and stale.deleted_at is not None


def test_patch_memory_refresh_protocol_error_is_500_and_persists_audit(
    tmp_path,
) -> None:
    mem0 = ExplorerRouteMem0Client(
        {"mem-1": {"id": "different-id", "memory": "before"}}
    )
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    _index_route_memory(app, "mem-1")

    response = TestClient(app, raise_server_exceptions=False).patch(
        "/v1/memories/mem-1?project_id=repo-a&app_id=app-a",
        json={"text": "after"},
    )

    assert response.status_code == 500
    with app.state.session_factory() as session:
        event = session.scalar(
            select(Event).where(
                Event.project_id == "repo-a",
                Event.operation == "memory.update",
                Event.status == EventStatus.FAILED,
            )
        )
    assert event is not None


@pytest.mark.parametrize("failure_stage", ["update", "refresh"])
def test_patch_memory_decode_value_errors_at_call_boundaries_are_500(
    tmp_path,
    failure_stage: str,
) -> None:
    decode_error = ValueError(f"{failure_stage} response is not JSON")
    mem0 = ExplorerRouteMem0Client(
        {"mem-1": {"id": "mem-1", "memory": "before", "metadata": {}}}
    )
    if failure_stage == "update":
        mem0.update_error = decode_error
    else:
        mem0.get_error = decode_error
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    _index_route_memory(app, "mem-1")

    response = TestClient(app, raise_server_exceptions=False).patch(
        "/v1/memories/mem-1?project_id=repo-a&app_id=app-a",
        json={"text": "after"},
    )

    assert response.status_code == 500
    with app.state.session_factory() as session:
        event = session.scalar(
            select(Event).where(
                Event.project_id == "repo-a",
                Event.operation == "memory.update",
                Event.status == EventStatus.FAILED,
            )
        )
    assert event is not None


def test_memory_history_wrong_app_is_404_without_upstream_access(tmp_path) -> None:
    mem0 = ExplorerRouteMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    _index_route_memory(app, "mem-1")

    response = TestClient(app).get(
        "/v1/memories/mem-1/history?project_id=repo-a&app_id=app-b"
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Memory not found"}
    assert mem0.history_calls == []


def test_memory_history_returns_scoped_results(tmp_path) -> None:
    mem0 = ExplorerRouteMem0Client()
    mem0.history_response = {"history": [{"event": "UPDATE"}]}
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    _index_route_memory(app, "mem-1")

    response = TestClient(app).get(
        "/v1/memories/mem-1/history?project_id=repo-a&app_id=app-a"
    )

    assert response.status_code == 200
    assert response.json() == {"results": [{"event": "UPDATE"}]}
    assert mem0.history_calls == ["mem-1"]


def test_memory_history_protocol_error_is_500(tmp_path) -> None:
    mem0 = ExplorerRouteMem0Client()
    mem0.history_response = {"unexpected": []}
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    _index_route_memory(app, "mem-1")

    response = TestClient(app, raise_server_exceptions=False).get(
        "/v1/memories/mem-1/history?project_id=repo-a&app_id=app-a"
    )

    assert response.status_code == 500


def test_memory_history_decode_value_error_is_500(tmp_path) -> None:
    mem0 = ExplorerRouteMem0Client()
    mem0.history_response = ValueError("history response is not JSON")
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    _index_route_memory(app, "mem-1")

    response = TestClient(app, raise_server_exceptions=False).get(
        "/v1/memories/mem-1/history?project_id=repo-a&app_id=app-a"
    )

    assert response.status_code == 500


@pytest.mark.parametrize(
    "list_response",
    [
        {},
        {"results": [], "total": "unknown"},
        {"results": [{"memory": "missing id"}]},
    ],
)
def test_reconcile_protocol_errors_are_500(
    tmp_path,
    list_response: dict[str, Any],
) -> None:
    mem0 = ExplorerRouteMem0Client()
    mem0.list_response = list_response
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )

    response = TestClient(app, raise_server_exceptions=False).post(
        "/v1/projects/repo-a/memories/reconcile",
        json={"project_id": "repo-a", "app_id": "app-a"},
    )

    assert response.status_code == 500


def test_reconcile_list_decode_value_error_is_500(tmp_path) -> None:
    mem0 = ExplorerRouteMem0Client()
    mem0.list_response = ValueError("list response is not JSON")
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )

    response = TestClient(app, raise_server_exceptions=False).post(
        "/v1/projects/repo-a/memories/reconcile",
        json={"project_id": "repo-a", "app_id": "app-a"},
    )

    assert response.status_code == 500


def test_reconcile_rejects_path_project_mismatch_without_upstream_access(
    tmp_path,
) -> None:
    mem0 = ExplorerRouteMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )

    response = TestClient(app).post(
        "/v1/projects/repo-a/memories/reconcile?project_id=repo-b&app_id=app-a",
        json={},
    )

    assert response.status_code == 403
    assert mem0.list_calls == []


def test_reconcile_passes_runtime_adoption_gate(tmp_path) -> None:
    mem0 = ExplorerRouteMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
            allow_adopt_unscoped_memories=False,
        ),
        mem0_client=mem0,
    )

    response = TestClient(app).post(
        "/v1/projects/repo-a/memories/reconcile",
        json={
            "project_id": "repo-a",
            "app_id": "app-a",
            "adopt_unscoped": True,
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Unscoped memory adoption is disabled at runtime"
    }
    assert mem0.list_calls == []


def test_reconcile_passes_default_project_and_commits_adoption(tmp_path) -> None:
    mem0 = ExplorerRouteMem0Client()
    mem0.list_response = {
        "results": [{"id": "mem-1", "memory": "hello", "metadata": {}}]
    }
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
            MEM0_SIDECAR_ALLOW_ADOPT_UNSCOPED=True,
        ),
        mem0_client=mem0,
    )

    response = TestClient(app).post(
        "/v1/projects/repo-a/memories/reconcile",
        json={
            "project_id": "repo-a",
            "app_id": "app-a",
            "adopt_unscoped": True,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "scanned": 1,
        "indexed": 1,
        "skipped_unscoped": 0,
        "skipped_other_scope": 0,
        "stale_marked": 0,
    }
    assert mem0.list_calls == [{"top_k": 5000, "show_expired": True}]
    with app.state.session_factory() as session:
        adopted = MemoryIndexRepository(session).get_memory(
            project_id="repo-a",
            mem0_memory_id="mem-1",
            app_id="app-a",
        )
    assert adopted is not None
