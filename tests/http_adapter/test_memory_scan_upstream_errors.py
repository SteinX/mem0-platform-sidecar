from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.mem0_client.client import Mem0RestClient
from mem0_sidecar.store.models import MemoryIndex


@pytest.mark.parametrize("upstream_status", [500, 503, None])
def test_scan_preserves_safe_upstream_failure_response(
    tmp_path: Path, upstream_status: int | None
) -> None:
    calls: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if upstream_status is None:
            raise httpx.ReadTimeout("private fixture timeout", request=request)
        return httpx.Response(
            upstream_status, json={"detail": "private fixture failure"}
        )

    core = Mem0RestClient(
        base_url="http://mem0.local", transport=httpx.MockTransport(upstream)
    )
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'upstream-error.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
            client_auth_enabled=False,
        ),
        mem0_client=core,
    )
    timestamp = datetime(2026, 10, 10, tzinfo=UTC)
    with app.state.session_factory() as session:
        session.execute(
            insert(MemoryIndex),
            [
                {
                    "project_id": "repo-a",
                    "mem0_memory_id": f"mem-{index}",
                    "app_id": "app-a",
                    "metadata_projection_json": "{}",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "consolidation_state": "ACTIVE",
                }
                for index in range(2)
            ],
        )
        session.commit()

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/memories/scan",
            json={"project_id": "repo-a", "app_id": "app-a", "mode": "page"},
        )
        assert response.status_code == 502
        assert response.json() == {"detail": "Mem0 upstream request failed"}
        assert calls
        reads_after_failure = len(calls)
        count = client.post(
            "/v1/memories/scan",
            json={"project_id": "repo-a", "app_id": "app-a", "mode": "count"},
        )
        assert count.status_code == 200
        assert count.json()["total"] == 2
        assert len(calls) == reads_after_failure

    with app.state.session_factory() as session:
        assert session.scalars(select(MemoryIndex.deleted_at)).all() == [None, None]
