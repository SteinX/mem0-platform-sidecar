import sqlite3
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.mem0_client.client import Mem0RestClient


def test_query_commits_audit_event_while_another_connection_reads(
    tmp_path: Path,
) -> None:
    database = tmp_path / "sidecar.sqlite3"
    app = create_app(
        settings=SidecarSettings(database_url=f"sqlite:///{database}"),
    )
    with closing(sqlite3.connect(database)) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM projects").fetchall()
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/v1/memories/query",
                    json={"project_id": "default", "app_id": "test"},
                )
            assert response.status_code == 200, response.text
            assert reader.execute("SELECT count(*) FROM events").fetchone() == (0,)
        finally:
            reader.rollback()
    with closing(sqlite3.connect(database)) as observer:
        assert observer.execute(
            "SELECT status FROM events WHERE operation='memory.list'"
        ).fetchall() == [("SUCCEEDED",)]


@pytest.mark.parametrize("path", ["/v3/memories/add", "/memories"])
@pytest.mark.parametrize("upstream_status", [502, 503])
def test_provider_failure_returns_gateway_error_and_preserves_failed_event(
    tmp_path: Path,
    path: str,
    upstream_status: int,
) -> None:
    def fail_upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            upstream_status,
            json={"detail": "private provider credentials must not escape"},
        )

    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
        ),
        mem0_client=Mem0RestClient(
            base_url="http://upstream.test",
            transport=httpx.MockTransport(fail_upstream),
        ),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            path,
            json={
                "messages": [{"role": "user", "content": "test"}],
                "user_id": "root",
                "app_id": "test",
                "project_id": "default",
            },
        )
        assert response.status_code == 502, response.text
        assert response.json() == {"detail": "Mem0 upstream request failed"}
        assert response.headers["X-Request-ID"]
        events = client.get("/v1/events?project_wide=true").json()["results"]
        assert len(events) == 1
        assert events[0]["status"] == "FAILED"
