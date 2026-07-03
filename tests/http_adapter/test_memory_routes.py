import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.store.models import Event, EventStatus, Project
from mem0_sidecar.store.repositories import EventRepository, ProjectRepository


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
    assert mem0.add_payloads[0]["app_id"] == "repo-a"

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
    assert mem0.add_payloads[0]["app_id"] == "repo-b"
    assert "project_id" not in mem0.add_payloads[0]

    search_response = client.post(
        "/v3/memories/search/",
        json={"query": "hello", "user_id": "root", "project_id": "repo-c"},
    )
    assert search_response.status_code == 200
    assert mem0.search_payloads[0]["app_id"] == "repo-c"
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
    assert mem0.add_payloads[0]["app_id"] == "app-x"
    assert "project_id" not in mem0.add_payloads[0]

    with app.state.session_factory() as session:
        project_a = session.scalar(select(Project).where(Project.id == "repo-a"))
        event = session.scalar(
            select(Event).where(
                Event.project_id == "repo-a",
                Event.operation == "memory.add",
            )
        )

    assert project_a is not None
    assert event is not None


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
    assert mem0.add_payloads[0]["app_id"] == "app-x"
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
    assert mem0.search_payloads[0]["app_id"] == "app-x"
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
    assert mem0.search_payloads[0]["app_id"] == "app-x"
    assert "project_id" not in mem0.search_payloads[0]

    with app.state.session_factory() as session:
        project_a = session.scalar(select(Project).where(Project.id == "repo-a"))

    assert project_a is None


def test_route_scoped_search_uses_app_id_as_local_project_fallback_without_bootstrapping(
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
    assert mem0.add_payloads[0]["app_id"] == "app-x"
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


def test_route_scoped_requests_reject_conflicting_project_scope(tmp_path) -> None:
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

    event_response = client.get("/v1/event/does-not-matter?project_id=repo-a&app_id=repo-b")
    assert event_response.status_code == 404
