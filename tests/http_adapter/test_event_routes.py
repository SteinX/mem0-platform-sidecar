import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.store.models import Event, EventStatus, Project
from mem0_sidecar.store.repositories import EventRepository, ProjectRepository


class SearchTraceMem0Client:
    async def search_memories(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("query") == "explode":
            raise RuntimeError("search exploded")
        return {"results": []}


def _create_test_app(tmp_path, *, mem0_client: object | None = None):
    return create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.internal",
            default_project_id="repo-a",
        ),
        mem0_client=mem0_client or SearchTraceMem0Client(),
    )


def _seed_project(app, project_id: str, *, app_id: str) -> None:
    with app.state.session_factory() as session:
        ProjectRepository(session).upsert_default_project(
            project_id=project_id,
            name=project_id,
            mem0_base_url="http://mem0.internal",
            default_app_id=app_id,
        )
        session.commit()


def _seed_event(
    app,
    *,
    project_id: str = "repo-a",
    app_id: str = "app-a",
    operation: str = "memory.search",
    status: EventStatus = EventStatus.SUCCEEDED,
    created_at: datetime | None = None,
    user_id: str = "root",
    results: list[dict[str, Any]] | None = None,
) -> str:
    with app.state.session_factory() as session:
        events = EventRepository(session)
        event = events.create_event(
            project_id=project_id,
            app_id=app_id,
            user_id=user_id,
            operation=operation,
            request={
                "app_id": app_id,
                "user_id": user_id,
                "query": "hello",
            },
            correlation_id=f"request-{app_id}",
        )
        if status is EventStatus.SUCCEEDED:
            events.mark_succeeded(event.id, response={"results": results or []})
        elif status is EventStatus.FAILED:
            events.mark_failed(event.id, error={"message": "boom"})
        else:
            event.status = status
            session.flush()
        if created_at is not None:
            event.created_at = created_at
            event.started_at = created_at
        event_id = event.id
        session.commit()
    return event_id


def test_query_events_returns_scoped_paged_envelope_and_timeline(tmp_path) -> None:
    app = _create_test_app(tmp_path)
    _seed_project(app, "repo-a", app_id="app-a")
    older_id = _seed_event(
        app,
        created_at=datetime(2026, 7, 13, 10, 15, tzinfo=UTC),
    )
    newer_id = _seed_event(
        app,
        created_at=datetime(2026, 7, 13, 11, 15, tzinfo=UTC),
    )

    response = TestClient(app).post(
        "/v1/events/query",
        json={
            "project_id": "repo-a",
            "app_id": "app-a",
            "operation": "SEARCH",
            "statuses": ["SUCCEEDED"],
            "has_results": False,
            "date_range": {
                "from": "2026-07-13T09:00:00Z",
                "to": "2026-07-13T12:00:00Z",
            },
            "entity_filters": {"user_id": "root"},
            "page": 1,
            "page_size": 1,
        },
    )

    assert response.status_code == 200
    body = response.json()
    latency_ms = body["results"][0].pop("latency_ms")
    completed_at = body["results"][0].pop("completed_at")
    assert isinstance(latency_ms, int | float)
    assert latency_ms >= 0
    assert isinstance(completed_at, str)
    assert completed_at.endswith("Z")
    assert body == {
        "results": [
            {
                "id": newer_id,
                "correlation_id": "request-app-a",
                "operation": "memory.search",
                "display_operation": "SEARCH",
                "status": "SUCCEEDED",
                "entities": [
                    {"type": "user", "id": "root"},
                    {"type": "app", "id": "app-a"},
                ],
                "request": {"query": "hello"},
                "response": {},
                "error": {},
                "result_count": 0,
                "has_results": False,
                "requested_at": "2026-07-13T11:15:00Z",
                "result_previews": [],
                "result_previews_omitted": 0,
                "result_previews_scan_truncated": False,
            }
        ],
        "total": 2,
        "page": 1,
        "page_size": 1,
        "has_more": True,
        "timeline": [
            {"timestamp": "2026-07-13T10:00:00Z", "count": 1},
            {"timestamp": "2026-07-13T11:00:00Z", "count": 1},
        ],
    }
    assert older_id != newer_id
    assert "project_id" not in body["results"][0]
    assert "app_id" not in body["results"][0]["request"]


@pytest.mark.parametrize("payload", [[], "SEARCH", None])
def test_query_events_rejects_non_object_json(tmp_path, payload) -> None:
    app = _create_test_app(tmp_path)

    response = TestClient(app).post("/v1/events/query", json=payload)

    assert response.status_code == 422


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation", "DELETE"),
        ("operation", "search"),
        ("statuses", "SUCCEEDED"),
        ("statuses", ["SUCCESS"]),
        ("statuses", [1]),
        ("statuses", ["SUCCEEDED"] * 6),
        ("has_results", "true"),
        ("has_results", 1),
        ("date_range", "today"),
        ("date_range", {"from": "2026-07-13"}),
        (
            "date_range",
            {
                "from": "2026-07-14T00:00:00Z",
                "to": "2026-07-13T00:00:00Z",
            },
        ),
        ("page", 0),
        ("page", True),
        ("page_size", 0),
        ("page_size", 101),
        ("page_size", False),
        ("entity_filters", []),
        ("entity_filters", {"project_id": "repo-a"}),
        ("entity_filters", {"user_id": ""}),
        ("entity_filters", {"user_id": 7}),
    ],
)
def test_query_events_rejects_invalid_filters(tmp_path, field, value) -> None:
    app = _create_test_app(tmp_path)

    response = TestClient(app).post(
        "/v1/events/query",
        json={"project_id": "repo-a", field: value},
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "scope_payload",
    [
        {"project_id": ""},
        {"project_id": 7},
        {"project_id": None},
        {"app_id": ""},
        {"app_id": 7},
        {"app_id": None},
    ],
)
def test_query_events_rejects_explicit_invalid_scope_instead_of_defaulting(
    tmp_path,
    scope_payload,
) -> None:
    app = _create_test_app(tmp_path)

    response = TestClient(app).post("/v1/events/query", json=scope_payload)

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("display_operation", "stored_operation"),
    [
        ("ADD", "memory.add"),
        ("SEARCH", "memory.search"),
        ("GET_ALL", "memory.list"),
    ],
)
def test_query_events_normalizes_supported_operations(
    tmp_path,
    display_operation,
    stored_operation,
) -> None:
    app = _create_test_app(tmp_path)
    _seed_project(app, "repo-a", app_id="app-a")
    expected_id = _seed_event(app, operation=stored_operation)

    response = TestClient(app).post(
        "/v1/events/query",
        json={
            "project_id": "repo-a",
            "app_id": "app-a",
            "operation": display_operation,
        },
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["results"]] == [expected_id]


def test_query_events_isolates_project_and_app_scope(tmp_path) -> None:
    app = _create_test_app(tmp_path)
    _seed_project(app, "repo-a", app_id="app-a")
    _seed_project(app, "repo-b", app_id="app-a")
    visible_id = _seed_event(app, project_id="repo-a", app_id="app-a")
    _seed_event(app, project_id="repo-a", app_id="app-b")
    _seed_event(app, project_id="repo-b", app_id="app-a")

    response = TestClient(app).post(
        "/v1/events/query",
        json={"project_id": "repo-a", "app_id": "app-a"},
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["results"]] == [visible_id]
    assert response.json()["total"] == 1


def test_query_events_unknown_project_does_not_bootstrap(tmp_path) -> None:
    app = _create_test_app(tmp_path)

    response = TestClient(app).post(
        "/v1/events/query",
        json={"project_id": "repo-missing", "app_id": "app-a"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Project not found"}
    with app.state.session_factory() as session:
        assert session.get(Project, "repo-missing") is None


def test_event_detail_returns_404_for_another_project(tmp_path) -> None:
    app = _create_test_app(tmp_path)
    _seed_project(app, "repo-a", app_id="app-a")
    _seed_project(app, "repo-b", app_id="app-b")
    event_id = _seed_event(app, project_id="repo-b", app_id="app-b")

    response = TestClient(app).get(
        f"/v1/event/{event_id}",
        params={"project_id": "repo-a"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Event not found"}


def test_query_events_exposes_successful_and_failed_search_traces(tmp_path) -> None:
    app = _create_test_app(tmp_path, mem0_client=SearchTraceMem0Client())
    client = TestClient(app, raise_server_exceptions=False)

    assert client.post(
        "/v3/memories/search",
        json={"query": "hello", "user_id": "root"},
    ).status_code == 200
    assert client.post(
        "/v3/memories/search",
        json={"query": "explode", "user_id": "root"},
    ).status_code == 500

    response = client.post(
        "/v1/events/query",
        json={"operation": "SEARCH", "page_size": 10},
    )

    assert response.status_code == 200
    traces = response.json()["results"]
    assert {trace["status"] for trace in traces} == {"SUCCEEDED", "FAILED"}
    assert all(trace["display_operation"] == "SEARCH" for trace in traces)
    failed = next(trace for trace in traces if trace["status"] == "FAILED")
    assert failed["error"]["message"] == "search exploded"


def test_query_events_filters_has_results_true_and_false(tmp_path) -> None:
    app = _create_test_app(tmp_path)
    _seed_project(app, "repo-a", app_id="app-a")
    with_results = _seed_event(
        app,
        results=[{"id": "mem-1", "memory": "hello", "app_id": "app-a"}],
    )
    without_results = _seed_event(app, results=[])
    client = TestClient(app)

    present = client.post(
        "/v1/events/query",
        json={"project_id": "repo-a", "app_id": "app-a", "has_results": True},
    )
    absent = client.post(
        "/v1/events/query",
        json={"project_id": "repo-a", "app_id": "app-a", "has_results": False},
    )

    assert [item["id"] for item in present.json()["results"]] == [with_results]
    assert [item["id"] for item in absent.json()["results"]] == [without_results]


def test_legacy_event_list_and_detail_resanitize_payloads(tmp_path) -> None:
    app = _create_test_app(tmp_path)
    with app.state.session_factory() as session:
        event = Event(
            project_id="repo-a",
            app_id="repo-a",
            operation="memory.search",
            status=EventStatus.SUCCEEDED,
            request_json=json.dumps(
                {
                    "app_id": "repo-a",
                    "Authorization": "Bearer legacy-secret",
                    "query": "hello",
                }
            ),
            response_json=json.dumps(
                {"token": "response-secret", "results": []}
            ),
            error_json=json.dumps({"cookie": "error-secret"}),
        )
        session.add(event)
        session.commit()
        event_id = event.id
    client = TestClient(app)

    listed = client.get("/v1/events", params={"project_id": "repo-a"})
    queried = client.post(
        "/v1/events/query",
        json={"project_id": "repo-a", "app_id": "repo-a"},
    )
    detail = client.get(f"/v1/event/{event_id}", params={"project_id": "repo-a"})

    assert listed.status_code == 200
    assert queried.status_code == 200
    assert detail.status_code == 200
    assert listed.json().keys() == {"results"}
    for response in (listed, queried, detail):
        assert "legacy-secret" not in response.text
        assert "response-secret" not in response.text
        assert "error-secret" not in response.text
    assert detail.json()["request"]["Authorization"] == "[REDACTED]"
    assert detail.json()["response"]["token"] == "[REDACTED]"
    assert detail.json()["error"]["cookie"] == "[REDACTED]"


def test_query_events_maps_repository_scan_limit_to_422(tmp_path, monkeypatch) -> None:
    app = _create_test_app(tmp_path)

    def reject_scan(self, project_id, app_id, query):
        raise ValueError("entity filter scan exceeds 5000 records")

    monkeypatch.setattr(EventRepository, "query_project_events", reject_scan)

    response = TestClient(app).post("/v1/events/query", json={})

    assert response.status_code == 422
    assert response.json() == {
        "detail": "entity filter scan exceeds 5000 records"
    }


def test_query_events_validates_entity_filters_before_repository_call(
    tmp_path,
    monkeypatch,
) -> None:
    app = _create_test_app(tmp_path)
    called = False

    def record_call(self, project_id, app_id, query):
        nonlocal called
        called = True
        raise AssertionError("repository must not receive invalid scope")

    monkeypatch.setattr(EventRepository, "query_project_events", record_call)

    response = TestClient(app).post(
        "/v1/events/query",
        json={"entity_filters": {"agent_id": "x" * 257}},
    )

    assert response.status_code == 422
    assert called is False
