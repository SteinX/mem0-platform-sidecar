import json

import httpx
import pytest
from fastapi.testclient import TestClient

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.mem0_client.client import Mem0RestClient
from mem0_sidecar.store.repositories import MemoryIndexRepository


@pytest.mark.parametrize("path", ["/search", "/v3/memories/search/"])
def test_history_control_keeps_scope_and_does_not_reach_core(tmp_path, path):
    requests = []

    async def core(request):
        requests.append(json.loads(request.read()))
        return httpx.Response(200, json={"results": [
            {"id": key, "memory": key, "score": 0.9}
            for key in ("current", "merged", "superseded", "foreign")
        ]})

    app = create_app(settings=SidecarSettings(
        database_url=f"sqlite:///{tmp_path / 'current.db'}",
        default_project_id="project-a", default_app_id="app-a",
    ), mem0_client=Mem0RestClient(
        base_url="http://core.test", transport=httpx.MockTransport(core),
    ))
    with app.state.session_factory() as session:
        repo = MemoryIndexRepository(session)
        for key, state in (
            ("current", "ACTIVE"), ("merged", "MERGED"),
            ("superseded", "SUPERSEDED"), ("foreign", "MERGED"),
        ):
            repo.upsert_memory(
                project_id="project-a", mem0_memory_id=key,
                app_id="app-b" if key == "foreign" else "app-a",
                user_id="user-a", category=None,
            )
            stored = repo.get_memory(project_id="project-a", mem0_memory_id=key)
            stored.consolidation_state = state
        session.commit()
    payload = {
        "query": "fact", "project_id": "project-a",
        "app_id": "app-a", "user_id": "user-a",
    }
    with TestClient(app) as client:
        current = client.post(path, json=payload)
        history = client.post(path, json={**payload, "current_only": False})
        invalid = client.post(path, json={**payload, "current_only": "false"})
    assert current.status_code == history.status_code == 200
    assert [row["id"] for row in current.json()["results"]] == ["current"]
    assert [row["id"] for row in history.json()["results"]] == [
        "current", "merged", "superseded",
    ]
    assert invalid.status_code == 422
    assert len(requests) == 2 and all("current_only" not in row for row in requests)
