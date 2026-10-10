from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import insert

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.http_adapter.client_auth import ClientPrincipal
from mem0_sidecar.request_attribution import RequestAttribution
from mem0_sidecar.store.models import MemoryIndex


class EmptyCore:
    async def get_memory(self, memory_id: str) -> dict[str, object]:
        raise AssertionError(memory_id)


class CountingCore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_memory(self, memory_id: str) -> dict[str, object]:
        self.calls.append(memory_id)
        index = int(memory_id.removeprefix("mem-"))
        return {
            "id": memory_id,
            "memory": memory_id,
            "user_id": "alice",
            "app_id": "app-a",
            "metadata": {"type": "decision" if index >= 5000 else "Decision"},
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


def test_scan_count_route_is_cursor_v1_and_does_not_change_legacy_window(
    tmp_path,
) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'scan.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
            client_auth_enabled=False,
        ),
        mem0_client=EmptyCore(),
    )

    with TestClient(app) as client:
        scan = client.post(
            "/v1/memories/scan",
            json={
                "project_id": "repo-a",
                "app_id": "repo-a",
                "mode": "count",
                "filters": {},
            },
        )
        legacy = client.post(
            "/v1/memories/query",
            json={
                "project_id": "repo-a",
                "app_id": "repo-a",
                "page": 51,
                "page_size": 100,
            },
        )

    assert scan.status_code == 200
    assert scan.json() == {
        "protocol": "cursor-v1",
        "results": [],
        "total": 0,
        "count_basis": "sidecar_projection",
        "next_cursor": None,
        "has_more": False,
        "stale_skipped": 0,
    }
    assert legacy.status_code == 422
    assert legacy.json()["detail"] == "page window must not exceed 5000 records"


def test_http_8568_traversal_crosses_legacy_window_with_exact_type(tmp_path) -> None:
    core = CountingCore()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'scan-8568.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
            client_auth_enabled=False,
        ),
        mem0_client=core,
    )
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    with app.state.session_factory() as session:
        session.execute(
            insert(MemoryIndex),
            [
                {
                    "project_id": "repo-a",
                    "mem0_memory_id": f"mem-{index:05d}",
                    "user_id": "alice",
                    "app_id": "app-a",
                    "metadata_projection_json": (
                        '{"type":"decision"}'
                        if index >= 5000
                        else '{"type":"Decision"}'
                    ),
                    "created_at": created_at,
                    "updated_at": created_at,
                    "consolidation_state": "ACTIVE",
                }
                for index in range(8568)
            ],
        )
        session.commit()

    observed: list[str] = []
    cursor = None
    with TestClient(app) as client:
        count = client.post(
            "/v1/memories/scan",
            json={
                "project_id": "repo-a",
                "app_id": "app-a",
                "filters": {"user_id": "alice", "type": "decision"},
                "mode": "count",
            },
        )
        assert count.status_code == 200
        assert count.json()["total"] == 3568
        assert core.calls == []

        while True:
            before = len(core.calls)
            response = client.post(
                "/v1/memories/scan",
                json={
                    "project_id": "repo-a",
                    "app_id": "app-a",
                    "filters": {"user_id": "alice", "type": "decision"},
                    "mode": "page",
                    "page_size": 100,
                    **({"cursor": cursor} if cursor is not None else {}),
                },
            )
            assert response.status_code == 200
            page = response.json()
            assert page["protocol"] == "cursor-v1"
            assert page["total"] == 3568
            assert len(core.calls) - before <= 100
            observed.extend(item["id"] for item in page["results"])
            if not page["has_more"]:
                break
            assert page["next_cursor"] is not None
            cursor = page["next_cursor"]

    assert len(observed) == 3568
    assert len(set(observed)) == 3568
    assert observed[0] == "mem-05000"
    assert observed[-1] == "mem-08567"


def test_scan_cursor_rebinding_and_member_project_wide_are_rejected(tmp_path) -> None:
    core = CountingCore()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'scope.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
            client_auth_enabled=False,
        ),
        mem0_client=core,
    )
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    with app.state.session_factory() as session:
        session.execute(
            insert(MemoryIndex),
            [
                {
                    "project_id": "repo-a",
                    "mem0_memory_id": f"mem-{index:05d}",
                    "user_id": "alice",
                    "app_id": "app-a",
                    "metadata_projection_json": '{"type":"Decision"}',
                    "created_at": created_at,
                    "updated_at": created_at,
                    "consolidation_state": "ACTIVE",
                }
                for index in range(2)
            ],
        )
        session.commit()

    with TestClient(app) as client:
        first = client.post(
            "/v1/memories/scan",
            json={
                "project_id": "repo-a",
                "app_id": "app-a",
                "filters": {},
                "mode": "page",
                "page_size": 1,
            },
        )
        rebound = client.post(
            "/v1/memories/scan",
            json={
                "project_id": "repo-a",
                "app_id": "app-b",
                "filters": {},
                "mode": "page",
                "page_size": 1,
                "cursor": first.json()["next_cursor"],
            },
        )
        malformed = client.post(
            "/v1/memories/scan",
            json={
                "project_id": "repo-a",
                "app_id": "app-a",
                "filters": {},
                "mode": "page",
                "cursor": "not-base64!",
            },
        )

    assert first.status_code == 200
    assert rebound.status_code == 422
    assert rebound.json()["detail"] == (
        "memory scan cursor does not match request scope"
    )
    assert malformed.status_code == 422

    member_app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'member.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
            client_auth_enabled=True,
        ),
        mem0_client=core,
        client_auth_verifier=MemberVerifier(),
    )
    project_wide = TestClient(member_app).post(
        "/v1/memories/scan",
        json={
            "project_id": "repo-a",
            "project_wide": True,
            "filters": {},
            "mode": "count",
        },
    )
    assert project_wide.status_code == 403

