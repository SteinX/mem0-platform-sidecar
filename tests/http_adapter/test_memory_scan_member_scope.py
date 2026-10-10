from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import insert

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.core.memory_scan_types import JsonObject
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.http_adapter.client_auth import ClientPrincipal
from mem0_sidecar.request_attribution import RequestAttribution
from mem0_sidecar.store.models import MemoryIndex, Project


class MemberCore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_memory(self, memory_id: str) -> JsonObject:
        self.calls.append(memory_id)
        return {
            "id": memory_id,
            "memory": memory_id,
            "user_id": "alice",
            "app_id": "app-a",
            "metadata": {"type": "Decision"},
        }


class MemberVerifier:
    async def verify(
        self,
        *,
        authorization: str | None,
        x_api_key: str | None,
        caller_context: str | None = None,
    ) -> ClientPrincipal:
        return ClientPrincipal(
            subject_id="member",
            role="member",
            attribution=RequestAttribution(
                transport="rest",
                credential_kind="core_api_key",
                credential_id="e0544e3c-d217-40d9-bc9a-c1f64077542a",
                credential_label="member-test",
                credential_prefix="m0sk_test_",
            ),
        )


def test_member_scan_uses_default_scope_and_rejects_overrides(tmp_path: Path) -> None:
    core = MemberCore()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'member-default.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
            client_auth_enabled=True,
        ),
        mem0_client=core,
        client_auth_verifier=MemberVerifier(),
    )
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    with app.state.session_factory() as session:
        project = session.get(Project, "repo-a")
        assert project is not None
        project.default_app_id = "app-a"
        session.execute(
            insert(MemoryIndex),
            [
                {
                    "project_id": project_id,
                    "mem0_memory_id": memory_id,
                    "user_id": "alice",
                    "app_id": app_id,
                    "metadata_projection_json": '{"type":"Decision"}',
                    "created_at": created_at,
                    "updated_at": created_at,
                    "consolidation_state": "ACTIVE",
                }
                for memory_id, project_id, app_id in (
                    ("mem-00001", "repo-a", "app-a"),
                    ("mem-00002", "repo-a", "app-a"),
                    ("mem-00003", "repo-a", "app-foreign"),
                    ("mem-00004", "repo-b", "app-b"),
                )
            ],
        )
        session.commit()

    headers = {"X-API-Key": "member-key"}
    with TestClient(app) as client:
        count = client.post(
            "/v1/memories/scan", headers=headers, json={"mode": "count"}
        )
        first = client.post(
            "/v1/memories/scan",
            headers=headers,
            json={"mode": "page", "page_size": 1},
        )
        assert first.status_code == 200
        assert first.json()["next_cursor"] is not None
        second = client.post(
            "/v1/memories/scan",
            headers=headers,
            json={
                "mode": "page",
                "page_size": 1,
                "cursor": first.json()["next_cursor"],
            },
        )
        selectors = [
            client.post(
                "/v1/memories/scan",
                headers=headers,
                json={**selector, "mode": "count"},
            )
            for selector in (
                {"project_id": "repo-a"},
                {"app_id": "app-a"},
                {"project_wide": True},
            )
        ]
        query_selector = client.post(
            "/v1/memories/scan?project_id=repo-a",
            headers=headers,
            json={"mode": "count"},
        )
        incompatible_selectors = client.post(
            "/v1/memories/scan",
            headers=headers,
            json={"app_id": "app-a", "project_wide": True, "mode": "count"},
        )

    assert count.status_code == 200
    assert count.json()["total"] == 2
    assert first.json()["total"] == 2
    assert [item["id"] for item in first.json()["results"]] == ["mem-00001"]
    assert second.status_code == 200
    assert second.json()["total"] == 2
    assert [item["id"] for item in second.json()["results"]] == ["mem-00002"]
    assert [response.status_code for response in selectors] == [403, 403, 403]
    assert query_selector.status_code == 403
    assert incompatible_selectors.status_code == 422
    assert core.calls == ["mem-00001", "mem-00002"]


def test_system_scan_requires_explicit_app_or_project_wide(tmp_path: Path) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'system-scope.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
            client_auth_enabled=False,
        ),
        mem0_client=MemberCore(),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/memories/scan",
            json={"project_id": "repo-a", "mode": "count"},
        )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "exactly one of app_id or project_wide=true is required"
    }
