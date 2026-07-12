from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.core.dashboard_categories import CategoryAdminService
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


def test_put_project_categories_rejects_missing_categories_field(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)

    response = client.put(
        "/v1/projects/default/categories",
        json={},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Categories must be a list of category objects"


def test_category_item_routes_create_patch_delete(tmp_path):
    client = TestClient(_app(tmp_path))
    create_response = client.post(
        "/v1/projects/default/categories",
        json={
            "name": "preferences",
            "description": "Durable preferences",
            "schema": {"type": "object"},
            "enabled": True,
            "strategy": "metadata",
        },
    )
    assert create_response.status_code == 201
    category = create_response.json()

    patch_response = client.patch(
        f"/v1/projects/default/categories/{category['id']}",
        json={"description": "Updated", "enabled": False},
    )
    assert patch_response.status_code == 200
    assert patch_response.json()["description"] == "Updated"
    assert patch_response.json()["enabled"] is False
    assert patch_response.json()["version"] == 2

    delete_response = client.delete(
        f"/v1/projects/default/categories/{category['id']}"
    )
    assert delete_response.status_code == 204
    assert client.get("/v1/projects/default/categories").json()["categories"] == []


def test_category_item_routes_reject_duplicate_names(tmp_path):
    client = TestClient(_app(tmp_path))
    first = client.post(
        "/v1/projects/default/categories", json={"name": "work", "schema": {}}
    )
    second = client.post(
        "/v1/projects/default/categories", json={"name": "work", "schema": {}}
    )
    assert first.status_code == 201
    assert second.status_code == 400
    assert second.json()["detail"] == "Category names must be unique per project"


def test_create_category_maps_unique_constraint_race_to_400(tmp_path, monkeypatch):
    client = TestClient(_app(tmp_path))
    assert client.post(
        "/v1/projects/default/categories", json={"name": "work", "schema": {}}
    ).status_code == 201
    monkeypatch.setattr(
        CategoryAdminService,
        "_ensure_unique_name",
        lambda self, project_id, name, category_id=None: None,
    )

    response = client.post(
        "/v1/projects/default/categories", json={"name": "work", "schema": {}}
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Category names must be unique per project"
    assert len(client.get("/v1/projects/default/categories").json()["categories"]) == 1


def test_rename_category_maps_unique_constraint_race_to_400(tmp_path, monkeypatch):
    client = TestClient(_app(tmp_path))
    first = client.post(
        "/v1/projects/default/categories", json={"name": "work", "schema": {}}
    ).json()
    second = client.post(
        "/v1/projects/default/categories", json={"name": "personal", "schema": {}}
    ).json()
    monkeypatch.setattr(
        CategoryAdminService,
        "_ensure_unique_name",
        lambda self, project_id, name, category_id=None: None,
    )

    response = client.patch(
        f"/v1/projects/default/categories/{second['id']}", json={"name": "work"}
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Category names must be unique per project"
    categories = client.get("/v1/projects/default/categories").json()["categories"]
    assert {category["id"]: category["name"] for category in categories} == {
        first["id"]: "work",
        second["id"]: "personal",
    }


def test_create_category_does_not_misclassify_other_integrity_errors(
    tmp_path, monkeypatch
):
    def raise_unrelated_error(self, *, project_id, item):
        raise IntegrityError(
            "INSERT INTO categories",
            {},
            Exception("FOREIGN KEY constraint failed"),
        )

    monkeypatch.setattr(CategoryAdminService, "create_category", raise_unrelated_error)
    client = TestClient(_app(tmp_path))

    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        client.post(
            "/v1/projects/default/categories", json={"name": "work", "schema": {}}
        )


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        ({"name": "  "}, "Category name is required"),
        ({"name": "work", "schema": []}, "Category schema must be a JSON object"),
    ],
)
def test_create_category_rejects_invalid_payload(tmp_path, payload, detail):
    response = TestClient(_app(tmp_path)).post(
        "/v1/projects/default/categories", json=payload
    )
    assert response.status_code == 400
    assert response.json()["detail"] == detail


@pytest.mark.parametrize("method", ["patch", "delete"])
def test_category_item_routes_return_404_for_missing_id(tmp_path, method):
    client = TestClient(_app(tmp_path))
    response = getattr(client, method)(
        "/v1/projects/default/categories/missing",
        **({"json": {"enabled": False}} if method == "patch" else {}),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Category not found"


def test_category_item_routes_are_project_scoped(tmp_path):
    client = TestClient(_app(tmp_path))
    created = client.post(
        "/v1/projects/alpha/categories", json={"name": "work", "schema": {}}
    ).json()
    response = client.patch(
        f"/v1/projects/beta/categories/{created['id']}",
        json={"enabled": False},
    )
    assert response.status_code == 404


def test_legacy_bulk_put_still_replaces_categories(tmp_path):
    client = TestClient(_app(tmp_path))
    client.post(
        "/v1/projects/default/categories", json={"name": "old", "schema": {}}
    )
    response = client.put(
        "/v1/projects/default/categories",
        json={"categories": [{"name": "new", "schema": {}}]},
    )
    assert response.status_code == 200
    assert [item["name"] for item in response.json()["categories"]] == ["new"]


def test_legacy_bulk_put_can_replace_a_category_with_the_same_name(tmp_path):
    client = TestClient(_app(tmp_path))
    client.post(
        "/v1/projects/default/categories",
        json={"name": "work", "description": "Before", "schema": {}},
    )

    response = client.put(
        "/v1/projects/default/categories",
        json={
            "categories": [
                {"name": "work", "description": "After", "schema": {}}
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["categories"][0]["description"] == "After"
