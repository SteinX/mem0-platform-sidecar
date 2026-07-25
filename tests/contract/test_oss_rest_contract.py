import json
from pathlib import Path
from typing import Any

import pytest

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app

FIXTURES = Path(__file__).parent / "fixtures"


class _UnusedMem0Client:
    pass


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
