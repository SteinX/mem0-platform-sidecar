from collections.abc import Mapping


def extract_category(
    metadata: Mapping[str, object], configured_names: set[str]
) -> str | None:
    candidates: list[object] = [
        metadata.get("type"),
        metadata.get("category"),
        metadata.get("custom_category"),
    ]
    categories_value = metadata.get("categories")
    if isinstance(categories_value, list):
        candidates.extend(categories_value)

    for candidate in candidates:
        if isinstance(candidate, str) and candidate in configured_names:
            return candidate
    return None
