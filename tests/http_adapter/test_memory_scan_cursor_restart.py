import base64
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import JsonValue
from sqlalchemy import insert

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.core.memory_scan_types import JsonObject
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.store.models import MemoryIndex


class CursorCore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_memory(self, memory_id: str) -> JsonObject:
        self.calls.append(memory_id)
        return {"id": memory_id, "memory": memory_id, "metadata": {}}


def _create_scan_app(database_url: str, core: CursorCore, secret: str | None):
    return create_app(
        settings=SidecarSettings(
            database_url=database_url,
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
            client_auth_enabled=False,
            memory_cursor_secret=secret,
        ),
        mem0_client=core,
    )


def _seed_two(app) -> None:
    created_at = datetime(2026, 10, 10, tzinfo=UTC)
    with app.state.session_factory() as session:
        session.execute(
            insert(MemoryIndex),
            [
                {
                    "project_id": "repo-a",
                    "mem0_memory_id": f"mem-{index}",
                    "app_id": "app-a",
                    "metadata_projection_json": "{}",
                    "created_at": created_at,
                    "updated_at": created_at,
                    "consolidation_state": "ACTIVE",
                }
                for index in range(2)
            ],
        )
        session.commit()


def _first_cursor(app) -> str:
    with TestClient(app) as client:
        response = client.post(
            "/v1/memories/scan",
            json={
                "project_id": "repo-a",
                "app_id": "app-a",
                "mode": "page",
                "page_size": 1,
                "filters": {},
            },
        )
    assert response.status_code == 200
    cursor = response.json()["next_cursor"]
    assert isinstance(cursor, str)
    return cursor


def _replace_cursor_field(token: str, field: str, value: JsonValue) -> str:
    padded = token + "=" * (-len(token) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded))
    payload[field] = value
    return base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()


def test_tampered_cursor_state_returns_422_without_core_reads(tmp_path) -> None:
    core = CursorCore()
    app = _create_scan_app(
        f"sqlite:///{tmp_path / 'tamper.sqlite3'}",
        core,
        "shared-memory-cursor-secret-value",
    )
    _seed_two(app)
    cursor = _first_cursor(app)
    core.calls.clear()
    mutations: dict[str, JsonValue] = {
        "upper": ["2099-01-01T00:00:00+00:00", "mem-99999"],
        "after": ["2026-10-10T00:00:00+00:00", "mem--1"],
        "snapshot_at": "2099-01-01T00:00:00+00:00",
        "total": 999,
    }

    with TestClient(app) as client:
        statuses = [
            client.post(
                "/v1/memories/scan",
                json={
                    "project_id": "repo-a",
                    "app_id": "app-a",
                    "mode": "page",
                    "page_size": 1,
                    "filters": {},
                    "cursor": _replace_cursor_field(cursor, field, value),
                },
            ).status_code
            for field, value in mutations.items()
        ]

    assert statuses == [422, 422, 422, 422]
    assert core.calls == []


@pytest.mark.parametrize(
    ("secret", "expected_status"),
    ((None, 422), ("shared-memory-cursor-secret-value", 200)),
)
def test_cursor_continuation_across_app_restart_requires_shared_secret(
    tmp_path, secret: str | None, expected_status: int
) -> None:
    database_url = f"sqlite:///{tmp_path / f'scan-{expected_status}.sqlite3'}"
    core = CursorCore()
    first_app = _create_scan_app(database_url, core, secret)
    _seed_two(first_app)
    cursor = _first_cursor(first_app)
    core.calls.clear()

    restarted_app = _create_scan_app(database_url, core, secret)
    with TestClient(restarted_app) as client:
        response = client.post(
            "/v1/memories/scan",
            json={
                "project_id": "repo-a",
                "app_id": "app-a",
                "mode": "page",
                "page_size": 1,
                "filters": {},
                "cursor": cursor,
            },
        )

    assert response.status_code == expected_status
    assert len(core.calls) == (1 if expected_status == 200 else 0)
