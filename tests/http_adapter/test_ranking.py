import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.mem0_client.client import Mem0RestClient
from mem0_sidecar.store.models import MemoryIndex
from mem0_sidecar.store.repositories import MemoryIndexRepository


@pytest.mark.parametrize("nested", [False, True])
def test_independent_ranking_signals_event_time_and_reversible_policy(tmp_path, nested):
    requests = []
    facts = [
        ("old", 0.9, "2000-01-01", False, "2020-03-01"),
        ("new", 0.85, "2099-01-01", False, "2026-10-01"),
        ("rule", 0.88, "2000-01-01", True, None),
        ("weak", 0.1, "2099-01-01", False, "2026-10-01"),
        ("foreign", 1.0, "2099-01-01", False, "2020-03-01"),
    ]

    async def core(request):
        requests.append(json.loads(request.read()))
        rows = []
        for key, score, created, _, _ in facts:
            record = {"id": key, "memory": key, "created_at": created + "T00:00:00Z"}
            rows.append(
                {"memory": record, "score": score}
                if nested
                else {
                    **record,
                    "score": score,
                }
            )
        return httpx.Response(200, json={"results": rows})

    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'ranking.db'}",
            default_project_id="project-a",
            default_app_id="app-a",
        ),
        mem0_client=Mem0RestClient(
            base_url="http://core.test",
            transport=httpx.MockTransport(core),
        ),
    )
    with app.state.session_factory() as session:
        repo = MemoryIndexRepository(session)
        for key, _, _, evergreen, occurred in facts:
            repo.upsert_memory(
                project_id="project-a",
                app_id="app-b" if key == "foreign" else "app-a",
                mem0_memory_id=key,
                user_id="user-a",
                category=None,
                metadata={
                    "evergreen": evergreen,
                    **({"event_date": occurred} if occurred else {}),
                },
                observed_at=datetime(2026, 10, 19, tzinfo=UTC),
            )
        session.commit()
    payload = {
        "query": "facts",
        "project_id": "project-a",
        "app_id": "app-a",
        "user_id": "user-a",
        "top_k": 4,
    }
    with TestClient(app) as client:
        path = "/v1/projects/project-a/ranking-policy"

        def search(**extra):
            response = client.post("/search", json={**payload, **extra})
            assert response.status_code == 200, response.text
            return [
                {
                    **row["memory"],
                    **{key: value for key, value in row.items() if key != "memory"},
                }
                if isinstance(row.get("memory"), dict)
                else row
                for row in response.json()["results"]
            ]

        baseline = search()
        assert [row["id"] for row in baseline] == ["old", "new", "rule", "weak"]
        recency = client.put(
            path,
            json={
                "expected_version": 0,
                "settings": {"enabled": True, "recency_weight": 0.25},
            },
        )
        assert recency.status_code == 200, recency.text
        aged = search()
        assert [row["id"] for row in aged] == ["rule", "new", "old", "weak"]
        assert aged[0]["ranking_details"]["evergreen"] is True
        assert aged[2]["rank_score"] >= 0.9 * 0.75
        assert aged[2]["score"] == 0.9
        with app.state.session_factory() as session:
            old = session.query(MemoryIndex).filter_by(mem0_memory_id="old").one()
            old.access_count = 100
            session.commit()
        access = client.put(
            path,
            json={
                "expected_version": 1,
                "settings": {"enabled": True, "access_weight": 0.25},
            },
        )
        assert access.status_code == 200, access.text
        accessed = search()
        assert accessed[0]["id"] == "old"
        assert accessed[0]["ranking_details"]["recency_penalty"] == 0
        assert 0 < accessed[0]["ranking_details"]["access_bonus"] < 0.25
        dated = search(event_from="2020-01-01", event_to="2020-12-31")
        assert [row["id"] for row in dated] == ["old"]
        invalid = client.post(
            "/search",
            json={**payload, "event_from": "2027-01-01", "event_to": "2020-01-01"},
        )
        assert invalid.status_code == 422
        overbound = client.post("/search", json={**payload, "top_k": 201})
        assert overbound.status_code == 422
        stale = client.put(path, json={"expected_version": 0, "settings": {}})
        assert stale.status_code == 409
        disabled = client.put(
            path, json={"expected_version": 2, "settings": {"enabled": False}}
        )
        assert disabled.status_code == 200, disabled.text
        restored = search()
        assert restored == baseline
    assert all(
        "event_from" not in request and "event_to" not in request
        for request in requests
    )
    with app.state.session_factory() as session:
        foreign = session.query(MemoryIndex).filter_by(mem0_memory_id="foreign").one()
        assert foreign.access_count == 0 and foreign.last_accessed_at is None
        old = session.query(MemoryIndex).filter_by(mem0_memory_id="old").one()
        assert old.access_count == 102 and old.last_accessed_at is not None
        assert old.last_observed_at == datetime(2026, 10, 19)


@pytest.mark.parametrize(
    "settings",
    [
        {"recency_weight": 0.26},
        {"access_weight": -0.1},
        {"access_weight": "0.1"},
        {"candidate_limit": 201},
        {"half_life_days": True},
        {"enabled": "true"},
    ],
)
def test_ranking_policy_rejects_unbounded_or_coerced_settings(tmp_path, settings):
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'validation.db'}",
        )
    )
    with TestClient(app) as client:
        response = client.put(
            "/v1/projects/default/ranking-policy",
            json={
                "expected_version": 0,
                "settings": settings,
            },
        )
    assert response.status_code == 422
