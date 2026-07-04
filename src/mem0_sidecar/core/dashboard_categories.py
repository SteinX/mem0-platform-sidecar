import json
from typing import Any

from mem0_sidecar.store.models import Category
from mem0_sidecar.store.repositories import CategoryRepository


class CategoryValidationError(ValueError):
    pass


def normalize_category_payload(item: dict[str, Any]) -> dict[str, Any]:
    name = str(item.get("name", "")).strip()
    if not name:
        raise CategoryValidationError("Category name is required")

    schema = item.get("schema", {})
    if schema is None:
        schema = {}
    if not isinstance(schema, dict):
        raise CategoryValidationError("Category schema must be a JSON object")

    return {
        "name": name,
        "description": str(item.get("description", "")),
        "schema": schema,
        "enabled": bool(item.get("enabled", True)),
        "strategy": str(item.get("strategy", "metadata")),
    }


def category_to_dict(category: Category) -> dict[str, Any]:
    return {
        "id": category.id,
        "project_id": category.project_id,
        "name": category.name,
        "description": category.description,
        "schema": json.loads(category.schema_json),
        "enabled": bool(category.enabled),
        "strategy": category.strategy,
        "version": category.version,
        "created_at": category.created_at.isoformat(),
        "updated_at": category.updated_at.isoformat(),
    }


class CategoryAdminService:
    def __init__(self, repository: CategoryRepository) -> None:
        self.repository = repository

    def list_categories(self, project_id: str) -> dict[str, Any]:
        categories = self.repository.list_project_categories(project_id)
        return {"categories": [category_to_dict(category) for category in categories]}

    def replace_categories(
        self,
        *,
        project_id: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        normalized = [normalize_category_payload(item) for item in items]
        names = [item["name"] for item in normalized]
        if len(names) != len(set(names)):
            raise CategoryValidationError("Category names must be unique per project")

        repository_items = [
            {
                "name": item["name"],
                "description": item["description"],
                "schema": item["schema"],
                "enabled": item["enabled"],
                "strategy": item["strategy"],
            }
            for item in normalized
        ]
        categories = self.repository.replace_project_categories(
            project_id=project_id,
            categories=repository_items,
        )
        return {"categories": [category_to_dict(category) for category in categories]}
