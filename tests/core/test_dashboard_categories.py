import pytest

from mem0_sidecar.core.dashboard_categories import (
    CategoryAdminService,
    CategoryValidationError,
    normalize_category_patch,
    normalize_category_payload,
)
from mem0_sidecar.store.repositories import CategoryRepository, ProjectRepository


def test_normalize_category_payload_accepts_json_schema_object():
    normalized = normalize_category_payload(
        {
            "name": "preferences",
            "description": "Durable user preferences",
            "schema": {"type": "object"},
            "enabled": True,
            "strategy": "metadata",
        }
    )

    assert normalized == {
        "name": "preferences",
        "description": "Durable user preferences",
        "schema": {"type": "object"},
        "enabled": True,
        "strategy": "metadata",
    }


def test_normalize_category_payload_rejects_empty_name():
    with pytest.raises(CategoryValidationError, match="Category name is required"):
        normalize_category_payload({"name": "   ", "schema": {}})


def test_normalize_category_payload_rejects_non_object_schema():
    with pytest.raises(CategoryValidationError, match="schema must be a JSON object"):
        normalize_category_payload({"name": "work", "schema": ["bad"]})


def test_normalize_category_patch_keeps_only_supplied_fields():
    assert normalize_category_patch(
        {"description": "Updated", "enabled": False}
    ) == {"description": "Updated", "enabled": False}


def test_normalize_category_patch_rejects_empty_name():
    with pytest.raises(CategoryValidationError, match="Category name is required"):
        normalize_category_patch({"name": "  "})


def test_normalize_category_patch_rejects_non_object_schema():
    with pytest.raises(CategoryValidationError, match="schema must be a JSON object"):
        normalize_category_patch({"schema": ["invalid"]})


def test_normalize_category_patch_rejects_empty_patch():
    with pytest.raises(
        CategoryValidationError, match="At least one category field is required"
    ):
        normalize_category_patch({})


def test_category_service_preserves_advanced_schema(db_session):
    ProjectRepository(db_session).upsert_default_project(
        project_id="default", name="default", mem0_base_url="http://mem0:8000"
    )
    service = CategoryAdminService(CategoryRepository(db_session))
    schema = {
        "type": "object",
        "properties": {"score": {"oneOf": [{"type": "number"}, {"type": "null"}]}},
    }

    created = service.create_category(
        project_id="default",
        item={"name": "advanced", "schema": schema},
    )

    assert created["schema"] == schema


def test_category_service_partial_update_preserves_unspecified_fields(db_session):
    ProjectRepository(db_session).upsert_default_project(
        project_id="default", name="default", mem0_base_url="http://mem0:8000"
    )
    service = CategoryAdminService(CategoryRepository(db_session))
    created = service.create_category(
        project_id="default",
        item={
            "name": "preferences",
            "description": "Durable preferences",
            "schema": {"type": "object"},
            "enabled": True,
            "strategy": "metadata",
        },
    )

    updated = service.update_category(
        project_id="default",
        category_id=created["id"],
        item={"description": "Updated", "enabled": False},
    )

    assert updated["id"] == created["id"]
    assert updated["name"] == "preferences"
    assert updated["description"] == "Updated"
    assert updated["schema"] == {"type": "object"}
    assert updated["enabled"] is False
    assert updated["strategy"] == "metadata"
    assert updated["version"] == 2


def test_category_service_rejects_duplicate_name(db_session):
    ProjectRepository(db_session).upsert_default_project(
        project_id="default", name="default", mem0_base_url="http://mem0:8000"
    )
    service = CategoryAdminService(CategoryRepository(db_session))
    service.create_category(
        project_id="default", item={"name": "preferences", "schema": {}}
    )

    with pytest.raises(
        CategoryValidationError, match="Category names must be unique per project"
    ):
        service.create_category(
            project_id="default", item={"name": "preferences", "schema": {}}
        )


def test_category_service_missing_update_and_delete_raise_key_error(db_session):
    ProjectRepository(db_session).upsert_default_project(
        project_id="default", name="default", mem0_base_url="http://mem0:8000"
    )
    service = CategoryAdminService(CategoryRepository(db_session))

    with pytest.raises(KeyError):
        service.update_category(
            project_id="default",
            category_id="missing",
            item={"description": "Updated"},
        )
    with pytest.raises(KeyError):
        service.delete_category(project_id="default", category_id="missing")
