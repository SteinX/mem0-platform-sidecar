from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.core.memory_scan_types import JsonObject
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.store.models import MemoryIndex


class UnusedCore:
    async def get_memory(self, memory_id: str) -> JsonObject:
        raise AssertionError(f"invalid/count scope must not hydrate {memory_id}")


@pytest.mark.parametrize(
    ("project_id", "app_id", "project_wide"),
    [
        ("", "", False),
        ("", "repo-a", False),
        ("repo-a", "", False),
        ("", None, True),
        ("repo-a", "bad id", False),
        ("repo-a", "a" * 257, False),
        ("repo-a", "e\u0301", False),
        ("repo-a", "bad\x00id", False),
    ],
)
def test_explicit_scan_scope_cannot_fall_back_or_use_malformed_ids(
    tmp_path: Path, project_id: str, app_id: str | None, project_wide: bool
) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'explicit-scope.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
            client_auth_enabled=False,
        ),
        mem0_client=UnusedCore(),
    )
    timestamp = datetime(2026, 10, 10, tzinfo=UTC)
    with app.state.session_factory() as session:
        session.execute(
            insert(MemoryIndex),
            {
                "project_id": "repo-a",
                "mem0_memory_id": "default-memory",
                "app_id": "repo-a",
                "metadata_projection_json": "{}",
                "created_at": timestamp,
                "updated_at": timestamp,
                "consolidation_state": "ACTIVE",
            },
        )
        session.commit()

    with TestClient(app) as client:
        invalid = client.post(
            "/v1/memories/scan",
            json={
                "project_id": project_id,
                "app_id": app_id,
                "project_wide": project_wide,
                "mode": "count",
            },
        )
        assert invalid.status_code == 422
        valid = client.post(
            "/v1/memories/scan",
            json={"project_id": "repo-a", "app_id": "repo-a", "mode": "count"},
        )
        assert valid.status_code == 200
        assert valid.json()["total"] == 1
