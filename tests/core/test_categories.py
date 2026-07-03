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
