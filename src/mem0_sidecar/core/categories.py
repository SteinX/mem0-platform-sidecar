import hashlib
from collections.abc import Mapping


def category_name_hash(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def extract_category(
    metadata: Mapping[str, object],
    configured_names: set[str],
    *,
    configured_name_hashes: set[str] | None = None,
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
        if isinstance(candidate, str):
            if candidate in configured_names or (
                configured_name_hashes is not None
                and category_name_hash(candidate) in configured_name_hashes
            ):
                return candidate
    return None
