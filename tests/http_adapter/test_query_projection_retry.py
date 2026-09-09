from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.mem0_client.client import Mem0RestClient
from mem0_sidecar.store.models import Event, MemoryIndex
from mem0_sidecar.store.repositories import MemoryIndexRepository


@pytest.mark.parametrize(
    ("changes", "status", "reads", "outcomes"),
    [
        (0, 200, 1, ["SUCCEEDED"]),
        (1, 200, 2, ["FAILED", "SUCCEEDED"]),
        (2, 409, 2, ["FAILED", "FAILED"]),
    ],
)
@pytest.mark.parametrize("project_wide", [False, True])
def test_list_retries_one_projection_change_with_a_fresh_snapshot(
    tmp_path: Path,
    changes: int,
    status: int,
    reads: int,
    outcomes: list[str],
    project_wide: bool,
) -> None:
    # Given a real projection whose version can change during upstream hydration.
    calls = 0
    changed_at = datetime(2030, 1, 1, tzinfo=UTC)
    factory: sessionmaker[Session]

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        assert request.method == "GET"
        assert request.url.path == "/memories/record-1"
        calls += 1
        if calls <= changes:
            with factory() as writer:
                writer.execute(
                    update(MemoryIndex)
                    .where(MemoryIndex.mem0_memory_id == "record-1")
                    .values(updated_at=changed_at + timedelta(seconds=calls))
                )
                writer.commit()
        return httpx.Response(
            200, json={"id": "record-1", "memory": "preserved content"}
        )

    app = create_app(
        settings=SidecarSettings(database_url=f"sqlite:///{tmp_path / 'db.sqlite3'}"),
        mem0_client=Mem0RestClient(
            base_url="http://upstream.test", transport=httpx.MockTransport(upstream)
        ),
    )
    factory = app.state.session_factory
    with factory() as session:
        MemoryIndexRepository(session).upsert_memory(
            project_id="default",
            mem0_memory_id="record-1",
            user_id="root",
            agent_id=None,
            app_id="test",
            run_id=None,
            category=None,
            metadata={},
        )
        session.commit()

    # When the HTTP list route reads the current page.
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/memories/query",
            json={
                "project_id": "default",
                "project_wide": project_wide,
                **({} if project_wide else {"app_id": "test"}),
                "page_size": 20,
            },
            headers={"X-Request-ID": "projection-retry-test"},
        )

    # Then at most one fresh read is allowed, with both attempts auditable.
    assert response.status_code == status, response.text
    assert calls == reads
    with factory() as session:
        events = session.query(Event).order_by(Event.created_at).all()
        assert [event.status.value for event in events] == outcomes
        assert {event.correlation_id for event in events} == {"projection-retry-test"}
    if status == 200:
        result = response.json()
        assert [record["id"] for record in result["results"]] == ["record-1"]
        assert result["results"][0]["memory"] == "preserved content"
        assert result["total"] == 1
        assert result["has_more"] is False


def test_list_does_not_retry_an_exhausted_hydration_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        assert request.method == "GET"
        calls += 1
        return httpx.Response(404, json={"detail": "missing"})

    monkeypatch.setattr("mem0_sidecar.core.memory_ops.EXPLORER_RECORD_HORIZON", 1)
    app = create_app(
        settings=SidecarSettings(database_url=f"sqlite:///{tmp_path / 'db.sqlite3'}"),
        mem0_client=Mem0RestClient(
            base_url="http://upstream.test", transport=httpx.MockTransport(upstream)
        ),
    )
    with app.state.session_factory() as session:
        for memory_id in ("record-1", "record-2"):
            MemoryIndexRepository(session).upsert_memory(
                project_id="default",
                mem0_memory_id=memory_id,
                user_id="root",
                agent_id=None,
                app_id="test",
                run_id=None,
                category=None,
                metadata={},
            )
        session.commit()

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/memories/query",
            json={"project_id": "default", "app_id": "test", "page_size": 1},
        )

    assert response.status_code == 409
    assert calls == 1
    with app.state.session_factory() as session:
        events = session.query(Event).all()
        assert len(events) == 1
        assert events[0].status.value == "FAILED"
