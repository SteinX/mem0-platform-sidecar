from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.store.models import Entity
from mem0_sidecar.store.repositories import EntityRepository, MemoryIndexRepository
from tests.core.memory_scan_fixtures import CountingCore, seed_memories


@pytest.mark.parametrize("conditional_race", [False, True])
def test_scan_stale_finalization_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conditional_race: bool,
) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'stale.sqlite3'}",
            default_project_id="repo-a",
            client_auth_enabled=False,
        ),
        mem0_client=CountingCore({}),
    )
    with app.state.session_factory() as session:
        seed_memories(session, 1)
        EntityRepository(session).rebuild_project_entities("repo-a", "app-a")
        session.commit()
    if conditional_race:

        def lose_race(*args: object, **kwargs: object) -> int:
            return 0

        monkeypatch.setattr(MemoryIndexRepository, "mark_stale_if_unchanged", lose_race)
    with TestClient(app) as client:
        response = client.post(
            "/v1/memories/scan",
            json={
                "project_id": "repo-a",
                "app_id": "app-a",
                "mode": "page",
                "page_size": 1,
            },
        )
    with app.state.session_factory() as session:
        entities = list(session.scalars(select(Entity)))
        memory = MemoryIndexRepository(session).get_memory(
            project_id="repo-a",
            mem0_memory_id="mem-00000",
            include_deleted=True,
        )
        assert memory is not None
        if conditional_race:
            assert response.status_code == 409
            assert "restart without a cursor" in response.json()["detail"]
            assert memory.deleted_at is None
            assert {row.memory_count for row in entities} == {1}
        else:
            assert response.status_code == 200
            assert response.json()["stale_skipped"] == 1
            assert memory.deleted_at is not None
            assert entities == []
