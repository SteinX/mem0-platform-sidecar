from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.http_adapter.client_auth import (
    ClientAuthenticationRejected,
    ClientPrincipal,
)
from mem0_sidecar.mem0_client.client import Mem0RestClient, Mem0UpstreamError
from mem0_sidecar.store.models import Event, Project
from mem0_sidecar.store.repositories import MemoryIndexRepository


class OssRouteMem0Client:
    def __init__(self) -> None:
        self.add_payloads: list[dict[str, Any]] = []
        self.deleted_ids: list[str] = []
        self.get_memory_ids: list[str] = []
        self.history_ids: list[str] = []
        self.list_params: list[dict[str, Any]] = []
        self.search_payloads: list[dict[str, Any]] = []
        self.update_calls: list[tuple[str, dict[str, Any]]] = []

    async def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.add_payloads.append(payload)
        return {
            "results": [
                {
                    "id": "mem-oss-1",
                    "memory": "Prefers tea",
                    "event": "ADD",
                }
            ]
        }

    async def get_memory(self, memory_id: str) -> dict[str, Any]:
        self.get_memory_ids.append(memory_id)
        return {
            "id": memory_id,
            "memory": "Prefers tea",
            "user_id": "u1",
            "metadata": {},
        }

    async def search_memories(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.search_payloads.append(payload)
        return {
            "results": [
                {"id": "mem-oss-1", "memory": "Prefers tea", "score": 0.91}
            ]
        }

    async def search_memories_raw(
        self,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        self.search_payloads.append(payload)
        return [{"id": "mem-oss-1", "memory": "Prefers tea", "score": 0.91}]

    async def list_memories(self, params: dict[str, Any]) -> dict[str, Any]:
        self.list_params.append(params)
        return {
            "results": [{"id": "mem-oss-1", "memory": "Prefers tea"}],
            "total": 1,
        }

    async def list_memories_raw(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.list_params.append(params)
        return [{"id": "mem-oss-1", "memory": "Prefers tea"}]

    async def get_memory_history(self, memory_id: str) -> dict[str, Any]:
        self.history_ids.append(memory_id)
        return {
            "results": [
                {
                    "id": "history-1",
                    "memory_id": memory_id,
                    "event": "ADD",
                }
            ]
        }

    async def get_memory_history_raw(
        self,
        memory_id: str,
    ) -> list[dict[str, Any]]:
        self.history_ids.append(memory_id)
        return [
            {
                "id": "history-1",
                "memory_id": memory_id,
                "event": "ADD",
            }
        ]

    async def update_memory(
        self,
        memory_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.update_calls.append((memory_id, payload))
        return {"message": "Memory updated successfully"}

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        self.deleted_ids.append(memory_id)
        return {"message": "Memory deleted successfully"}


class MemberVerifier:
    async def verify(
        self,
        *,
        authorization: str | None,
        x_api_key: str | None,
    ) -> ClientPrincipal:
        return ClientPrincipal(
            subject_id="member-1",
            role="member",
            credential_kind="api_key",
        )


class RejectingVerifier:
    async def verify(
        self,
        *,
        authorization: str | None,
        x_api_key: str | None,
    ) -> ClientPrincipal:
        raise ClientAuthenticationRejected(
            status_code=401,
            detail="Authentication required.",
        )


class FailingBulkDeleteMem0Client(OssRouteMem0Client):
    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        self.deleted_ids.append(memory_id)
        raise Mem0UpstreamError(
            method="DELETE",
            path=f"/memories/{memory_id}",
            status_code=500,
            response_text="retry",
            outcome_unknown=False,
            message="retry",
        )


class RejectingSearchMem0Client(OssRouteMem0Client):
    async def search_memories_raw(self, payload: dict[str, Any]) -> Any:
        raise Mem0UpstreamError(
            method="POST",
            path="/search",
            status_code=422,
            response_text='{"detail":[{"loc":["body","top_k"],"msg":"too large"}]}',
            outcome_unknown=False,
            message="upstream rejected search",
        )


def _app(tmp_path, mem0: OssRouteMem0Client):
    return create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="default-project",
        ),
        mem0_client=mem0,
    )


def test_oss_add_returns_core_shape_and_creates_trace(tmp_path) -> None:
    mem0 = OssRouteMem0Client()
    app = _app(tmp_path, mem0)

    response = TestClient(app).post(
        "/memories",
        headers={
            "X-Request-ID": "oss-add-1",
            "X-Mem0-Project-ID": "project-from-header",
            "X-Mem0-App-ID": "app-from-header",
        },
        json={
            "messages": [{"role": "user", "content": "Prefers tea"}],
            "user_id": "u1",
            "project_id": "project-from-body",
            "app_id": "app-from-body",
            "metadata": {"type": "preference"},
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "results": [
            {
                "id": "mem-oss-1",
                "memory": "Prefers tea",
                "event": "ADD",
            }
        ]
    }
    assert response.headers["X-Request-ID"] == "oss-add-1"
    assert mem0.add_payloads == [
        {
            "messages": [{"role": "user", "content": "Prefers tea"}],
            "user_id": "u1",
            "metadata": {
                "type": "preference",
                "_mem0_sidecar_project_id": "project-from-header",
                "_mem0_sidecar_app_id": "app-from-header",
                "_mem0_sidecar_mutation_id": mem0.add_payloads[0]["metadata"][
                    "_mem0_sidecar_mutation_id"
                ],
            },
        }
    ]

    with app.state.session_factory() as session:
        event = session.scalar(
            select(Event).where(Event.operation == "memory.add")
        )
        assert event is not None
        assert event.project_id == "project-from-header"
        assert event.app_id == "app-from-header"
        assert event.user_id == "u1"
        assert event.correlation_id == "oss-add-1"


def test_oss_search_uses_filter_scope_and_top_level_entity_precedence(
    tmp_path,
) -> None:
    mem0 = OssRouteMem0Client()
    app = _app(tmp_path, mem0)
    with app.state.session_factory() as session:
        MemoryIndexRepository(session).upsert_memory(
            project_id="default-project",
            mem0_memory_id="mem-oss-1",
            user_id="top-level-user",
            app_id="app-from-filter",
            category=None,
            metadata={},
        )
        session.commit()

    response = TestClient(app).post(
        "/search",
        headers={"X-Request-ID": "oss-search-1"},
        json={
            "query": "tea",
            "user_id": "top-level-user",
            "filters": {
                "user_id": "filter-user",
                "app_id": "app-from-filter",
                "project_id": "default-project",
            },
            "top_k": 10,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == [
        {"id": "mem-oss-1", "memory": "Prefers tea", "score": 0.91}
    ]
    assert mem0.search_payloads == [
        {
            "query": "tea",
            "user_id": "top-level-user",
            "filters": {
                "user_id": "filter-user",
                "_mem0_sidecar_project_id": "default-project",
                "_mem0_sidecar_app_id": "app-from-filter",
            },
            "top_k": 10,
        }
    ]

    with app.state.session_factory() as session:
        event = session.scalar(
            select(Event).where(Event.operation == "memory.search")
        )
        assert event is not None
        assert event.project_id == "default-project"
        assert event.app_id == "app-from-filter"
        assert event.user_id == "top-level-user"
        assert event.correlation_id == "oss-search-1"


def test_oss_list_returns_scoped_core_shape_and_creates_trace(tmp_path) -> None:
    mem0 = OssRouteMem0Client()
    app = _app(tmp_path, mem0)
    with app.state.session_factory() as session:
        MemoryIndexRepository(session).upsert_memory(
            project_id="default-project",
            mem0_memory_id="mem-oss-1",
            user_id="u1",
            app_id="app-a",
            category=None,
            metadata={},
        )
        session.commit()

    response = TestClient(app).get(
        "/memories",
        headers={"X-Request-ID": "oss-list-1"},
        params={
            "user_id": "u1",
            "app_id": "app-a",
            "top_k": 10,
            "show_expired": "true",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == [{"id": "mem-oss-1", "memory": "Prefers tea"}]
    assert mem0.list_params == [
        {
            "user_id": "u1",
            "top_k": 10,
            "show_expired": True,
        }
    ]

    with app.state.session_factory() as session:
        event = session.scalar(
            select(Event).where(Event.operation == "memory.list")
        )
        assert event is not None
        assert event.app_id == "app-a"
        assert event.user_id == "u1"
        assert event.correlation_id == "oss-list-1"


def test_oss_get_returns_core_shape_and_creates_trace(tmp_path) -> None:
    mem0 = OssRouteMem0Client()
    app = _app(tmp_path, mem0)
    with app.state.session_factory() as session:
        MemoryIndexRepository(session).upsert_memory(
            project_id="default-project",
            mem0_memory_id="mem-oss-1",
            user_id="u1",
            app_id="app-a",
            category=None,
            metadata={},
        )
        session.commit()

    response = TestClient(app).get(
        "/memories/mem-oss-1",
        headers={"X-Request-ID": "oss-get-1"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "id": "mem-oss-1",
        "memory": "Prefers tea",
        "user_id": "u1",
        "metadata": {},
    }
    assert mem0.get_memory_ids == ["mem-oss-1"]

    with app.state.session_factory() as session:
        event = session.scalar(
            select(Event).where(Event.operation == "memory.get")
        )
        assert event is not None
        assert event.app_id == "app-a"
        assert event.subject_id == "mem-oss-1"
        assert event.correlation_id == "oss-get-1"


def test_oss_history_returns_core_shape_and_creates_trace(tmp_path) -> None:
    mem0 = OssRouteMem0Client()
    app = _app(tmp_path, mem0)
    with app.state.session_factory() as session:
        MemoryIndexRepository(session).upsert_memory(
            project_id="default-project",
            mem0_memory_id="mem-oss-1",
            user_id="u1",
            app_id="app-a",
            category=None,
            metadata={},
        )
        session.commit()

    response = TestClient(app).get(
        "/memories/mem-oss-1/history",
        headers={
            "X-Request-ID": "oss-history-1",
            "X-Mem0-App-ID": "app-a",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "id": "history-1",
            "memory_id": "mem-oss-1",
            "event": "ADD",
        }
    ]
    assert mem0.history_ids == ["mem-oss-1"]

    with app.state.session_factory() as session:
        event = session.scalar(
            select(Event).where(Event.operation == "memory.history")
        )
        assert event is not None
        assert event.app_id == "app-a"
        assert event.subject_id == "mem-oss-1"
        assert event.correlation_id == "oss-history-1"


def test_oss_update_preserves_core_response_and_refreshes_projection(
    tmp_path,
) -> None:
    mem0 = OssRouteMem0Client()
    app = _app(tmp_path, mem0)
    with app.state.session_factory() as session:
        MemoryIndexRepository(session).upsert_memory(
            project_id="default-project",
            mem0_memory_id="mem-oss-1",
            user_id="u1",
            app_id="app-a",
            category=None,
            metadata={},
        )
        session.commit()

    response = TestClient(app).put(
        "/memories/mem-oss-1",
        headers={
            "X-Request-ID": "oss-update-1",
            "X-Mem0-App-ID": "app-a",
        },
        json={"text": "Prefers green tea"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"message": "Memory updated successfully"}
    assert mem0.update_calls == [
        ("mem-oss-1", {"text": "Prefers green tea"})
    ]
    assert mem0.get_memory_ids == ["mem-oss-1"]

    with app.state.session_factory() as session:
        event = session.scalar(
            select(Event).where(Event.operation == "memory.update")
        )
        assert event is not None
        assert event.app_id == "app-a"
        assert event.subject_id == "mem-oss-1"
        assert event.correlation_id == "oss-update-1"


def test_oss_delete_preserves_core_response_and_creates_trace(tmp_path) -> None:
    mem0 = OssRouteMem0Client()
    app = _app(tmp_path, mem0)
    with app.state.session_factory() as session:
        MemoryIndexRepository(session).upsert_memory(
            project_id="default-project",
            mem0_memory_id="mem-oss-1",
            user_id="u1",
            app_id="app-a",
            category=None,
            metadata={},
        )
        session.commit()

    response = TestClient(app).delete(
        "/memories/mem-oss-1",
        headers={
            "X-Request-ID": "oss-delete-1",
            "X-Mem0-App-ID": "app-a",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"message": "Memory deleted successfully"}
    assert mem0.deleted_ids == ["mem-oss-1"]

    with app.state.session_factory() as session:
        event = session.scalar(
            select(Event).where(Event.operation == "memory.delete")
        )
        assert event is not None
        assert event.app_id == "app-a"
        assert event.subject_id == "mem-oss-1"
        assert event.correlation_id == "oss-delete-1"


def test_oss_bulk_delete_uses_durable_service_and_core_response(tmp_path) -> None:
    mem0 = OssRouteMem0Client()
    app = _app(tmp_path, mem0)
    with app.state.session_factory() as session:
        MemoryIndexRepository(session).upsert_memory(
            project_id="default-project",
            mem0_memory_id="mem-oss-1",
            user_id="u1",
            app_id="app-a",
            category=None,
            metadata={},
        )
        session.commit()

    response = TestClient(app).delete(
        "/memories",
        headers={"X-Request-ID": "oss-bulk-delete-1"},
        params={"user_id": "u1", "app_id": "app-a"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"message": "All relevant memories deleted"}
    assert mem0.deleted_ids == ["mem-oss-1"]

    with app.state.session_factory() as session:
        event = session.scalar(
            select(Event).where(Event.operation == "memory.bulk_delete")
        )
        assert event is not None
        assert event.app_id == "app-a"
        assert event.user_id == "u1"
        assert event.correlation_id == "oss-bulk-delete-1"


def test_oss_bulk_delete_rejects_non_admin_before_upstream_call(tmp_path) -> None:
    mem0 = OssRouteMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            default_project_id="default-project",
            client_auth_enabled=True,
        ),
        mem0_client=mem0,
        client_auth_verifier=MemberVerifier(),
    )

    response = TestClient(app).delete(
        "/memories",
        headers={"X-API-Key": "member-key"},
        params={"user_id": "u1"},
    )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Bulk memory deletion requires an admin principal"
    }
    assert mem0.deleted_ids == []


def test_oss_member_cannot_override_default_project_or_app_scope(tmp_path) -> None:
    mem0 = OssRouteMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            default_project_id="default-project",
            client_auth_enabled=True,
        ),
        mem0_client=mem0,
        client_auth_verifier=MemberVerifier(),
    )

    response = TestClient(app).post(
        "/memories",
        headers={
            "X-API-Key": "member-key",
            "X-Mem0-Project-ID": "other-project",
        },
        json={
            "messages": [{"role": "user", "content": "Prefers tea"}],
            "user_id": "u1",
            "app_id": "other-app",
        },
    )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Project and app scope overrides require an admin principal"
    }
    assert mem0.add_payloads == []
    with app.state.session_factory() as session:
        assert session.get(Project, "other-project") is None


def test_oss_scoped_reads_match_pinned_core_v2_0_12_wire_shapes(
    tmp_path,
) -> None:
    golden = {
        "list": [{"id": "mem-oss-1", "memory": "Prefers tea"}],
        "search": [
            {"id": "mem-oss-1", "memory": "Prefers tea", "score": 0.91}
        ],
        "history": [
            {
                "id": "history-1",
                "memory_id": "mem-oss-1",
                "event": "ADD",
            }
        ],
    }

    async def core_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search":
            return httpx.Response(200, json=golden["search"])
        if request.url.path.endswith("/history"):
            return httpx.Response(200, json=golden["history"])
        return httpx.Response(200, json=golden["list"])

    app = _app(
        tmp_path,
        Mem0RestClient(
            base_url="http://mem0.local",
            transport=httpx.MockTransport(core_handler),
        ),
    )
    with app.state.session_factory() as session:
        MemoryIndexRepository(session).upsert_memory(
            project_id="default-project",
            mem0_memory_id="mem-oss-1",
            user_id="u1",
            app_id="app-a",
            category=None,
            metadata={},
        )
        session.commit()
    client = TestClient(app)

    listed = client.get(
        "/memories",
        params={"user_id": "u1", "app_id": "app-a"},
    )
    searched = client.post(
        "/search",
        json={"query": "tea", "user_id": "u1", "app_id": "app-a"},
    )
    history = client.get(
        "/memories/mem-oss-1/history",
        params={"app_id": "app-a"},
    )

    assert listed.status_code == searched.status_code == history.status_code == 200
    assert listed.json() == golden["list"]
    assert searched.json() == golden["search"]
    assert history.json() == golden["history"]


def test_oss_routes_use_declared_error_envelopes(tmp_path) -> None:
    mem0 = FailingBulkDeleteMem0Client()
    app = _app(tmp_path, mem0)
    with app.state.session_factory() as session:
        MemoryIndexRepository(session).upsert_memory(
            project_id="default-project",
            mem0_memory_id="mem-oss-1",
            user_id="u1",
            app_id="app-a",
            category=None,
            metadata={},
        )
        session.commit()
    client = TestClient(app)

    missing = client.get(
        "/memories/missing",
        headers={
            "X-Mem0-App-ID": "app-a",
            "X-Request-ID": "oss-missing-1",
        },
    )
    invalid = client.post(
        "/memories",
        json={"messages": [], "user_id": "u1"},
    )
    conflict = client.delete(
        "/memories",
        params={"user_id": "u1", "app_id": "app-a"},
    )

    assert missing.status_code == 404
    assert missing.json() == {"detail": "Memory not found"}
    assert invalid.status_code == 422
    assert invalid.json() == {"detail": "messages must be a non-empty list"}
    assert conflict.status_code == 409
    assert set(conflict.json()) == {"detail"}
    assert "retry required" in conflict.json()["detail"]
    with app.state.session_factory() as session:
        missing_event = session.scalar(
            select(Event).where(
                Event.operation == "memory.get",
                Event.correlation_id == "oss-missing-1",
            )
        )
        assert missing_event is not None
        assert missing_event.status == "FAILED"


def test_oss_validation_and_upstream_errors_match_core_statuses(tmp_path) -> None:
    app = _app(tmp_path, OssRouteMem0Client())
    client = TestClient(app)

    missing_entity = client.post(
        "/memories",
        json={"messages": [{"role": "user", "content": "Prefers tea"}]},
    )
    empty_query = client.post(
        "/search",
        json={"query": "", "user_id": "u1"},
    )
    null_filters = client.post(
        "/search",
        json={"query": "tea", "user_id": "u1", "filters": None},
    )

    assert missing_entity.status_code == 400
    assert empty_query.status_code == 400
    assert null_filters.status_code == 200

    rejecting_path = tmp_path / "rejecting"
    rejecting_path.mkdir()
    rejecting_app = _app(rejecting_path, RejectingSearchMem0Client())
    upstream_rejection = TestClient(rejecting_app).post(
        "/search",
        json={"query": "tea", "user_id": "u1", "top_k": 100_000},
    )

    assert upstream_rejection.status_code == 422
    assert upstream_rejection.json() == {
        "detail": [{"loc": ["body", "top_k"], "msg": "too large"}]
    }


def test_oss_routes_preserve_auth_rejection_envelope(tmp_path) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            client_auth_enabled=True,
        ),
        mem0_client=OssRouteMem0Client(),
        client_auth_verifier=RejectingVerifier(),
    )

    response = TestClient(app).post(
        "/search",
        json={"query": "tea", "user_id": "u1"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required."}
