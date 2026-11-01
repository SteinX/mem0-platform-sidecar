import hashlib

from mem0_sidecar.core.categories import extract_category


def test_extract_category_trusts_explicit_metadata_type() -> None:
    category = extract_category(
        metadata={"type": "decision"},
        configured_names={"decision", "task_learning"},
    )

    assert category == "decision"


def test_extract_category_ignores_unknown_values() -> None:
    category = extract_category(
        metadata={"category": "unknown"},
        configured_names={"decision"},
    )

    assert category is None


def test_extract_category_matches_configured_name_hash() -> None:
    name = "sk-abcdefghij"
    category = extract_category(
        metadata={"categories": [name]},
        configured_names=set(),
        configured_name_hashes={hashlib.sha256(name.encode("utf-8")).hexdigest()},
    )

    assert category == name
