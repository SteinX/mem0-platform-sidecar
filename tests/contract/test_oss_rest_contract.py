import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.store.repositories import MemoryIndexRepository

FIXTURES = Path(__file__).parent / "fixtures"


class _UnusedMem0Client:
    pass


class _ContractMem0Client:
    def __init__(self) -> None:
        self.add_payloads: list[dict[str, Any]] = []

    async def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.add_payloads.append(payload)
        return {
            "results": [
                {
                    "id": "add-memory",
                    "memory": "Prefers tea",
                    "event": "ADD",
                }
            ]
        }

    async def list_memories_raw(self, params: dict[str, Any]) -> Any:
        return [{"id": "contract-memory", "memory": "Prefers tea"}]

    async def search_memories_raw(self, payload: dict[str, Any]) -> Any:
        return [
            {
                "id": "contract-memory",
                "memory": "Prefers tea",
                "score": 0.91,
            }
        ]

    async def get_memory(self, memory_id: str) -> dict[str, Any]:
        return {
            "id": memory_id,
            "memory": "Prefers tea",
            "user_id": "u1",
            "metadata": {},
        }

    async def update_memory(
        self,
        memory_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {"message": "Memory updated successfully"}

    async def get_memory_history_raw(self, memory_id: str) -> Any:
        return [
            {
                "id": "history-1",
                "memory_id": memory_id,
                "event": "ADD",
            }
        ]

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        return {"message": "Memory deleted successfully"}


def _load_cases() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "oss_rest_v1_cases.json").read_text())


def _declared_routes(routes: list[Any]):
    for route in routes:
        included_router = getattr(route, "original_router", None)
        if included_router is not None:
            yield from _declared_routes(included_router.routes)
        else:
            yield route


@pytest.mark.contract
def test_every_declared_oss_rest_v1_route_has_a_sidecar_handler(tmp_path) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
        ),
        mem0_client=_UnusedMem0Client(),
    )
    declared = _load_cases()
    routes = {
        (method, route.path)
        for route in _declared_routes(app.routes)
        for method in getattr(route, "methods", set())
    }

    missing = {
        (case["method"], case["path_template"])
        for case in declared
        if (case["method"], case["path_template"]) not in routes
    }

    assert missing == set()


@pytest.mark.contract
def test_every_oss_rest_v1_case_declares_wire_compatibility() -> None:
    required_fields = {
        "method",
        "path_template",
        "case",
        "request",
        "expected_status",
        "response_type",
        "stable_keys",
        "expected_response",
        "error_statuses",
        "scope_precedence",
    }

    for case in _load_cases():
        assert set(case) == required_fields
        assert case["expected_status"] == 200
        assert case["response_type"] in {"array", "object"}
        assert isinstance(case["stable_keys"], list)
        assert 401 in case["error_statuses"]
        assert case["scope_precedence"][-1] == "server_default"


@pytest.mark.contract
@pytest.mark.parametrize(
    "case",
    _load_cases(),
    ids=lambda case: case["case"],
)
def test_declared_oss_rest_v1_case_is_executable(case, tmp_path) -> None:
    mem0 = _ContractMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            default_project_id="default",
        ),
        mem0_client=mem0,
    )
    with app.state.session_factory() as session:
        MemoryIndexRepository(session).upsert_memory(
            project_id="default",
            mem0_memory_id="contract-memory",
            user_id="u1",
            app_id="default",
            category=None,
            metadata={},
        )
        session.commit()

    path = case["path_template"].replace(
        "{memory_id}",
        "contract-memory",
    )
    response = TestClient(app).request(
        case["method"],
        path,
        **case["request"],
    )

    assert response.status_code == case["expected_status"], response.text
    body = response.json()
    expected_type = list if case["response_type"] == "array" else dict
    assert isinstance(body, expected_type)
    assert body == case["expected_response"]
    if isinstance(body, dict):
        assert set(case["stable_keys"]).issubset(body)
    if case["case"] == "messages_add":
        assert mem0.add_payloads[0]["metadata"][
            "_mem0_sidecar_project_id"
        ] == "default"
        assert mem0.add_payloads[0]["metadata"][
            "_mem0_sidecar_app_id"
        ] == "default"
