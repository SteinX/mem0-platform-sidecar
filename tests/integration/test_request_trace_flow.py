import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.core.memory_ops import (
    SIDECAR_APP_ID_METADATA_KEY,
    SIDECAR_PROJECT_ID_METADATA_KEY,
)
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.store.models import Event
from mem0_sidecar.store.repositories import MemoryIndexRepository


class TraceFlowMem0Client:
    def __init__(self, *, failure_secret: str) -> None:
        self.failure_secret = failure_secret
        self.records: dict[str, dict[str, Any]] = {}
        self.search_result_ids: list[str] = []
        self.add_count = 0

    async def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.add_count += 1
        memory_id = f"added-{self.add_count}"
        metadata = dict(payload.get("metadata") or {})
        record = {
            "id": memory_id,
            "memory": payload["text"],
            "metadata": metadata,
            "user_id": payload.get("user_id"),
            "agent_id": payload.get("agent_id"),
            "app_id": metadata.get(SIDECAR_APP_ID_METADATA_KEY),
            "run_id": payload.get("run_id"),
        }
        self.records[memory_id] = record
        return record

    async def search_memories(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if payload.get("query") == "force-failure":
            raise RuntimeError(
                "password="
                f"{self.failure_secret} at http://mem0.internal/private"
            )
        results = [self.records[memory_id] for memory_id in self.search_result_ids]
        return {"results": results, "total": len(results)}

    async def get_memory(self, memory_id: str) -> dict[str, Any]:
        return self.records[memory_id]


def _seed_preview_records(
    app,
    mem0: TraceFlowMem0Client,
    *,
    project_id: str,
    app_id: str,
    count: int,
) -> None:
    with app.state.session_factory() as session:
        memories = MemoryIndexRepository(session)
        for index in range(count):
            memory_id = f"preview-{app_id}-{index:02d}"
            metadata = {
                SIDECAR_PROJECT_ID_METADATA_KEY: project_id,
                SIDECAR_APP_ID_METADATA_KEY: app_id,
            }
            mem0.records[memory_id] = {
                "id": memory_id,
                "memory": f"preview {index}",
                "metadata": metadata,
                "user_id": "trace-user",
                "agent_id": "trace-agent",
                "app_id": app_id,
                "run_id": "trace-run",
            }
            memories.upsert_memory(
                project_id=project_id,
                mem0_memory_id=memory_id,
                user_id="trace-user",
                agent_id="trace-agent",
                app_id=app_id,
                run_id="trace-run",
                category="trace",
                metadata={"category": "trace"},
            )
            mem0.search_result_ids.append(memory_id)
        session.commit()


def _query_traces(
    client: TestClient,
    *,
    project_id: str,
    app_id: str,
    **filters: object,
) -> dict[str, Any]:
    response = client.post(
        "/v1/events/query",
        json={"project_id": project_id, "app_id": app_id, **filters},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_request_trace_flow_is_scoped_bounded_and_secret_safe(tmp_path) -> None:
    project_id = "trace-project"
    app_id = "trace-app"
    other_app_id = "trace-other-app"
    empty_app_id = "trace-empty-app"
    request_secret = "request-secret-value-8791"
    nested_secret = "nested-secret-value-5204"
    string_secret = "string-secret-value-3318"
    large_secret = "large-secret-value-8147"
    failure_secret = "failure-secret-value-4119"
    submitted_secrets = {
        request_secret,
        nested_secret,
        string_secret,
        large_secret,
        failure_secret,
    }
    mem0 = TraceFlowMem0Client(failure_secret=failure_secret)
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'trace-flow.sqlite3'}",
            mem0_base_url="http://mem0.internal",
            default_project_id=project_id,
        ),
        mem0_client=mem0,
    )
    client = TestClient(app, raise_server_exceptions=False)
    started_at = datetime.now(UTC) - timedelta(minutes=1)

    large_text = (
        "bounded trace payload "
        + ("x" * (70 * 1024))
        + f" password={large_secret}"
    )
    add_response = client.post(
        "/v3/memories/add/",
        headers={"X-Request-ID": "trace-add-correlation"},
        json={
            "project_id": project_id,
            "app_id": app_id,
            "user_id": "trace-user",
            "agent_id": "trace-agent",
            "run_id": "trace-run",
            "text": large_text,
            "infer": False,
            "metadata": {
                "Authorization": request_secret,
                "nested": {
                    "apiKey": nested_secret,
                    "note": f"token={string_secret}",
                    "upstream": "http://mem0.internal/private",
                },
            },
        },
    )
    assert add_response.status_code == 200, add_response.text

    _seed_preview_records(
        app,
        mem0,
        project_id=project_id,
        app_id=app_id,
        count=25,
    )
    _seed_preview_records(
        app,
        mem0,
        project_id=project_id,
        app_id=other_app_id,
        count=1,
    )

    for request_id in ("trace-search-one", "trace-search-two"):
        response = client.post(
            "/v3/memories/search/",
            headers={"X-Request-ID": request_id},
            json={
                "project_id": project_id,
                "app_id": app_id,
                "user_id": "trace-user",
                "agent_id": "trace-agent",
                "run_id": "trace-run",
                "query": "preview",
                "filters": {
                    "cookie": request_secret,
                    "safe": f"api_key={string_secret}",
                },
            },
        )
        assert response.status_code == 200, response.text
        assert len(response.json()["results"]) == 25
        assert all(item["app_id"] == app_id for item in response.json()["results"])

    failed_search = client.post(
        "/v3/memories/search/",
        headers={"X-Request-ID": "trace-search-failed"},
        json={
            "project_id": project_id,
            "app_id": app_id,
            "user_id": "trace-user",
            "query": "force-failure",
        },
    )
    assert failed_search.status_code == 500

    list_with_results = client.post(
        "/v1/memories/query",
        headers={"X-Request-ID": "trace-list-present"},
        json={
            "project_id": project_id,
            "app_id": app_id,
            "page": 1,
            "page_size": 20,
        },
    )
    assert list_with_results.status_code == 200, list_with_results.text
    assert list_with_results.json()["results"]

    list_without_results = client.post(
        "/v1/memories/query",
        headers={"X-Request-ID": "trace-list-empty"},
        json={"project_id": project_id, "app_id": empty_app_id},
    )
    assert list_without_results.status_code == 200, list_without_results.text
    assert list_without_results.json()["results"] == []

    other_scope_list = client.post(
        "/v1/memories/query",
        headers={"X-Request-ID": "trace-list-other-app"},
        json={"project_id": project_id, "app_id": other_app_id},
    )
    assert other_scope_list.status_code == 200, other_scope_list.text
    assert len(other_scope_list.json()["results"]) == 1

    another_project_add = client.post(
        "/v3/memories/add/",
        headers={"X-Request-ID": "trace-other-project"},
        json={
            "project_id": "trace-project-other",
            "app_id": app_id,
            "text": "other project",
            "infer": False,
        },
    )
    assert another_project_add.status_code == 200, another_project_add.text

    ended_at = datetime.now(UTC) + timedelta(minutes=1)
    successful_search_page_1 = _query_traces(
        client,
        project_id=project_id,
        app_id=app_id,
        operation="SEARCH",
        statuses=["SUCCEEDED"],
        date_range={
            "from": started_at.isoformat(),
            "to": ended_at.isoformat(),
        },
        page=1,
        page_size=1,
    )
    successful_search_page_2 = _query_traces(
        client,
        project_id=project_id,
        app_id=app_id,
        operation="SEARCH",
        statuses=["SUCCEEDED"],
        date_range={
            "from": started_at.isoformat(),
            "to": ended_at.isoformat(),
        },
        page=2,
        page_size=1,
    )
    assert successful_search_page_1["total"] == 2
    assert successful_search_page_1["has_more"] is True
    assert successful_search_page_2["has_more"] is False
    assert (
        successful_search_page_1["results"][0]["id"]
        != successful_search_page_2["results"][0]["id"]
    )
    assert successful_search_page_1["timeline"]

    failed_traces = _query_traces(
        client,
        project_id=project_id,
        app_id=app_id,
        operation="SEARCH",
        statuses=["FAILED"],
    )
    assert failed_traces["total"] == 1
    assert failed_traces["results"][0]["correlation_id"] == "trace-search-failed"
    assert failed_traces["results"][0]["has_results"] is False

    add_traces = _query_traces(
        client,
        project_id=project_id,
        app_id=app_id,
        operation="ADD",
    )
    assert add_traces["total"] == 1
    assert add_traces["results"][0]["correlation_id"] == "trace-add-correlation"

    list_traces = _query_traces(
        client,
        project_id=project_id,
        app_id=app_id,
        operation="GET_ALL",
        has_results=True,
    )
    assert list_traces["total"] == 1
    assert list_traces["results"][0]["correlation_id"] == "trace-list-present"
    empty_list_traces = _query_traces(
        client,
        project_id=project_id,
        app_id=empty_app_id,
        operation="GET_ALL",
        has_results=False,
    )
    assert empty_list_traces["total"] == 1

    visible_ids = {
        item["id"]
        for item in _query_traces(
            client,
            project_id=project_id,
            app_id=app_id,
        )["results"]
    }
    assert "trace-list-other-app" not in {
        item["correlation_id"]
        for item in _query_traces(
            client,
            project_id=project_id,
            app_id=app_id,
        )["results"]
    }
    assert visible_ids

    search_trace_id = successful_search_page_1["results"][0]["id"]
    detail_response = client.get(
        f"/v1/event/{search_trace_id}",
        params={"project_id": project_id, "app_id": app_id},
    )
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()
    assert {
        "id",
        "correlation_id",
        "operation",
        "display_operation",
        "status",
        "entities",
        "request",
        "response",
        "error",
        "result_count",
        "has_results",
        "latency_ms",
        "requested_at",
        "completed_at",
        "result_previews",
        "result_previews_omitted",
        "result_previews_scan_truncated",
    } <= set(detail)
    assert detail["display_operation"] == "SEARCH"
    assert detail["result_count"] == 25
    assert len(detail["result_previews"]) == 20
    assert detail["result_previews_omitted"] == 5
    assert all(
        preview.get("app_id") == app_id
        for preview in detail["result_previews"]
    )
    assert "project_id" not in detail["request"]
    assert "app_id" not in detail["request"]

    with app.state.session_factory() as session:
        malformed = Event(
            project_id=project_id,
            app_id=app_id,
            operation="memory.search",
            request_json="{not-json",
            response_json="[not-json",
            error_json="not-json",
        )
        session.add(malformed)
        session.commit()
        malformed_id = malformed.id

    malformed_response = client.get(
        f"/v1/event/{malformed_id}",
        params={"project_id": project_id, "app_id": app_id},
    )
    assert malformed_response.status_code == 200, malformed_response.text
    malformed_detail = malformed_response.json()
    assert malformed_detail["request"] == {"_trace_invalid_json": True}
    assert malformed_detail["response"] == {"_trace_invalid_json": True}
    assert malformed_detail["error"] == {"_trace_invalid_json": True}

    with app.state.session_factory() as session:
        raw_events = list(session.scalars(select(Event)))
    assert raw_events
    raw_trace_json = "\n".join(
        document
        for event in raw_events
        for document in (event.request_json, event.response_json, event.error_json)
    )
    for secret in submitted_secrets:
        assert secret not in raw_trace_json
    assert "http://mem0.internal" not in raw_trace_json
    assert "[REDACTED]" in raw_trace_json
    assert "[REDACTED_URL]" in raw_trace_json
    for event in raw_events:
        for document in (event.request_json, event.response_json, event.error_json):
            assert len(document.encode("utf-8")) <= 65_536
            if event.id != malformed_id:
                json.loads(document)
