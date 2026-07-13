import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.core.memory_ops import (
    SIDECAR_APP_ID_METADATA_KEY,
    SIDECAR_PROJECT_ID_METADATA_KEY,
)
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.mem0_client.client import Mem0UpstreamError
from mem0_sidecar.store.models import Event, EventStatus, MemoryIndex, Project
from mem0_sidecar.store.repositories import (
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
        self.update_calls: list[tuple[str, dict[str, Any]]] = []
        self.update_error: Exception | None = None
        self.history_calls: list[str] = []
        self.history_response: Any = {"results": []}
        self.list_calls: list[dict[str, Any]] = []
        self.list_response: Any = {"results": []}

    async def get_memory(self, memory_id: str) -> Any:
        self.get_memory_ids.append(memory_id)
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
        return self.history_response

    async def list_memories(self, params: dict[str, Any]) -> Any:
        self.list_calls.append(params)
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
        )
        event_repo.mark_succeeded(visible_event.id, response={"id": "mem-a"})
        hidden_event = event_repo.create_event(
            project_id="repo-b",
            operation="memory.add",
            request={"text": "hidden"},
            subject_type="memory",
            subject_id="mem-b",
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


def test_delete_memory_missing_index_uses_query_app_id_for_failed_event(
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

    assert project_a is not None
    assert project_a.default_app_id == "app-x"
    assert event is not None
    assert json.loads(event.request_json)["memory_id"] == "mem-1"
    assert json.loads(event.request_json)["app_id"] == "app-x"


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


def test_query_memories_uses_body_scope_and_returns_public_envelope(tmp_path) -> None:
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


def test_query_memories_does_not_bootstrap_unknown_project(tmp_path) -> None:
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

    assert response.status_code == 200
    assert response.json() == {
        "results": [],
        "page": 1,
        "page_size": 20,
        "total": 0,
        "has_more": False,
        "stale_skipped": 0,
    }
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
    assert len(commit_calls) == 1
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
    assert len(commit_calls) == 1
    assert len(rollback_calls) == 1
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
    assert len(commit_calls) == 1
    assert len(rollback_calls) == 1
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
