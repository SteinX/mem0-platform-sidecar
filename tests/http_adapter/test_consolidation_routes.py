import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.store.repositories import (
    ConsolidationPolicyRepository,
    ConsolidationProposalRepository,
    ConsolidationRunRepository,
    ProjectRepository,
)

NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)


def seed_consolidation(app):
    with app.state.session_factory() as session:
        ProjectRepository(session).upsert_default_project(
            project_id="repo-a",
            name="Repo A",
            mem0_base_url="http://mem0:8000",
            default_app_id="app-a",
        )
        policy = ConsolidationPolicyRepository(session).upsert(
            project_id="repo-a",
            app_id="app-a",
            policy={"enabled": True, "mode": "OBSERVE"},
        )
        run = ConsolidationRunRepository(session).create(policy, now=NOW)
        run.status = "SUCCEEDED"
        run.completed_at = NOW
        proposal = ConsolidationProposalRepository(session).create(
            run=run,
            proposal_key="proposal-key",
            kind="EXACT_DUPLICATE",
            source_ids=("mem-a", "mem-b"),
            canonical_id="mem-a",
            score=None,
            evidence={"hash_prefix": "abc123", "count": 2},
            status="PENDING",
        )
        session.commit()
        return run.id, proposal.id


def test_consolidation_routes_are_read_only_scoped_and_text_free(tmp_path) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        )
    )
    run_id, proposal_id = seed_consolidation(app)

    with TestClient(app) as client:
        status = client.get(
            "/v1/projects/repo-a/apps/app-a/consolidation"
        )
        run = client.get(
            f"/v1/projects/repo-a/apps/app-a/consolidation/runs/{run_id}"
        )
        proposals = client.get(
            f"/v1/projects/repo-a/apps/app-a/consolidation/runs/{run_id}/proposals"
        )
        cross_scope = client.get(
            f"/v1/projects/repo-a/apps/app-b/consolidation/runs/{run_id}"
        )
        oversized = client.get(
            f"/v1/projects/repo-a/apps/app-a/consolidation/runs/{run_id}/proposals",
            params={"page_size": 101},
        )
        post = client.post(
            "/v1/projects/repo-a/apps/app-a/consolidation",
            json={},
        )

    assert status.status_code == 200
    assert status.json()["policy"]["mode"] == "OBSERVE"
    assert run.status_code == 200 and run.json()["id"] == run_id
    assert proposals.status_code == 200
    assert proposals.json()["results"][0]["id"] == proposal_id
    assert {"memory", "text", "data"}.isdisjoint(
        proposals.json()["results"][0]["evidence"]
    )
    assert cross_scope.status_code == 404
    assert oversized.status_code == 422
    assert post.status_code == 405
    assert "memory body" not in json.dumps(proposals.json()).lower()
