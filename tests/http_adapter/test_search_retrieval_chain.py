import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.core.memory_scan_types import JsonObject
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.mem0_client.client import Mem0RestClient
from mem0_sidecar.store.repositories import MemoryIndexRepository


@pytest.mark.parametrize(
    ("path", "nested"),
    [
        ("/v3/memories/search/", False),
        ("/v3/memories/search/", True),
        ("/search", False),
    ],
)
@pytest.mark.parametrize(
    "controls",
    [
        {},
        {"rerank": True, "explain": True},
        {"rerank": False, "explain": False},
        {"rerank": True, "explain": False},
        {"rerank": False, "explain": True},
    ],
)
def test_search_preserves_controls_and_scores_while_isolating_scope(
    tmp_path: Path,
    path: str,
    nested: bool,
    controls: JsonObject,
) -> None:
    # Given a real Sidecar route/client and Core hits spanning every scope boundary.
    requests: list[JsonObject] = []
    rows: list[JsonObject] = []
    scores: JsonObject = {
        "score": 0.81,
        "rerank_score": 0.97,
        "score_details": {"vector": 0.81, "rerank": 0.97},
    }
    ids = [
        "allowed",
        "other-project",
        "other-app",
        "other-user",
        "other-agent",
        "other-run",
    ]
    for memory_id in ids:
        record: JsonObject = {
            "id": memory_id,
            "memory": "Mem0 2.2.1 部署到 /workspace/data/mem0，排查 HTTP 502",
        }
        rows.append({"memory": record, **scores} if nested else {**record, **scores})

    async def core(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/search"
        assert request.headers["x-api-key"] == "fixture-core-key"
        requests.append(json.loads(request.read()))
        return httpx.Response(200, json={"results": rows, "total": len(rows)})

    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'retrieval.sqlite3'}",
            mem0_base_url="http://core.test",
            default_project_id="fixture-project",
            default_app_id="fixture-app",
        ),
        mem0_client=Mem0RestClient(
            base_url="http://core.test",
            api_key="fixture-core-key",
            transport=httpx.MockTransport(core),
        ),
    )
    with app.state.session_factory() as session:
        repository = MemoryIndexRepository(session)
        for memory_id in ids:
            repository.upsert_memory(
                project_id="other-project"
                if memory_id == "other-project"
                else "fixture-project",
                mem0_memory_id=memory_id,
                app_id="other-app" if memory_id == "other-app" else "fixture-app",
                user_id="other-user" if memory_id == "other-user" else "fixture-user",
                agent_id="other-agent"
                if memory_id == "other-agent"
                else "fixture-agent",
                run_id="other-run" if memory_id == "other-run" else "fixture-run",
                category=None,
                metadata={},
            )
        session.commit()

    # When a scoped REST search requests explicit or omitted retrieval controls.
    with TestClient(app) as client:
        response = client.post(
            path,
            json={
                "query": "2.2.1 /workspace/data/mem0 HTTP 502",
                "project_id": "fixture-project",
                "app_id": "fixture-app",
                "user_id": "fixture-user",
                "agent_id": "fixture-agent",
                "run_id": "fixture-run",
                "top_k": 15,
                **controls,
            },
        )

    # Then HTTP preserves both controls, normalized scores and the scoped result.
    assert response.status_code == 200, response.text
    expected_row = {
        "id": "allowed",
        "memory": "Mem0 2.2.1 部署到 /workspace/data/mem0，排查 HTTP 502",
        **scores,
    }
    assert response.json() == {"results": [expected_row], "total": 1}
    assert requests == [
        {
            "query": "2.2.1 /workspace/data/mem0 HTTP 502",
            "user_id": "fixture-user",
            "agent_id": "fixture-agent",
            "run_id": "fixture-run",
            "top_k": 15,
            "filters": {
                "_mem0_sidecar_project_id": "fixture-project",
                "_mem0_sidecar_app_id": "fixture-app",
            },
            **controls,
        }
    ]
