import pytest

from mem0_sidecar.core.dashboard_categories import (
    CategoryValidationError,
    normalize_category_payload,
)


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
