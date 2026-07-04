from typing import Any

import pytest
from fastapi.testclient import TestClient

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app


class FakeMem0Client:
    async def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "mem-1", "memory": payload.get("text", "")}

    async def search_memories(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"results": []}

    async def get_memory(self, memory_id: str) -> dict[str, Any]:
        return {"id": memory_id, "memory": "hello"}

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        return {"message": f"Deleted {memory_id}"}


def _app(tmp_path):
    return create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="default",
        ),
        mem0_client=FakeMem0Client(),
    )


def test_put_and_get_project_categories(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)

    response = client.put(
        "/v1/projects/default/categories",
        json={
            "categories": [
                {
                    "name": "preferences",
                    "description": "Durable user preferences",
                    "schema": {"type": "object"},
                    "enabled": True,
                    "strategy": "metadata",
                }
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["categories"][0]["project_id"] == "default"
    assert body["categories"][0]["name"] == "preferences"
    assert body["categories"][0]["enabled"] is True
    assert body["categories"][0]["schema"] == {"type": "object"}

    response = client.get("/v1/projects/default/categories")
    assert response.status_code == 200
    assert response.json()["categories"][0]["name"] == "preferences"


def test_put_project_categories_round_trips_enabled_false(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)

    response = client.put(
        "/v1/projects/default/categories",
        json={
            "categories": [
                {
                    "name": "preferences",
                    "description": "Durable user preferences",
                    "schema": {"type": "object"},
                    "enabled": False,
                    "strategy": "metadata",
                }
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["categories"][0]["enabled"] is False

    response = client.get("/v1/projects/default/categories")
    assert response.status_code == 200
    assert response.json()["categories"][0]["enabled"] is False


@pytest.mark.parametrize(
    "categories",
    [
        "oops",
        {"name": "work", "schema": {}},
    ],
)
def test_put_project_categories_rejects_malformed_categories_payload(
    tmp_path,
    categories,
):
    app = _app(tmp_path)
    client = TestClient(app)

    response = client.put(
        "/v1/projects/default/categories",
        json={"categories": categories},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Categories must be a list of category objects"


def test_put_project_categories_rejects_duplicate_names(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)

    response = client.put(
        "/v1/projects/default/categories",
        json={
            "categories": [
                {"name": "work", "schema": {}},
                {"name": "work", "schema": {}},
            ]
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Category names must be unique per project"
