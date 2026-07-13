from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

ExplorerMatch = Literal["all", "any"]
ExplorerSort = Literal["created_at_desc", "created_at_asc"]

EXPLORER_RECORD_HORIZON = 5000
MAX_EXPLORER_FILTERS = 64
MAX_EXPLORER_IN_VALUES = 100
MAX_EXPLORER_VALUE_CHARS = 256

MEMORY_FILTER_FIELDS = {
    "entity_type",
    "user_id",
    "agent_id",
    "app_id",
    "run_id",
    "memory_id",
    "category",
    "metadata",
}

_SCALAR_OPERATORS = frozenset({"equals", "not_equals", "in"})
_FIELD_OPERATORS = {
    "entity_type": _SCALAR_OPERATORS,
    "category": _SCALAR_OPERATORS,
    "metadata": frozenset({"contains"}),
}
_KNOWN_OPERATORS = frozenset({"equals", "not_equals", "in", "contains"})
_ENTITY_TYPES = frozenset({"user", "agent", "app", "run"})
_SORTS = frozenset({"created_at_desc", "created_at_asc"})


@dataclass(frozen=True)
class ExplorerFilter:
    field: str
    operator: str
    value: object


@dataclass(frozen=True)
class ExplorerDateRange:
    from_at: datetime | None
    to_at: datetime | None


@dataclass(frozen=True)
class ExplorerQuery:
    match: ExplorerMatch
    filters: tuple[ExplorerFilter, ...]
    date_range: ExplorerDateRange
    page: int
    page_size: int
    sort: ExplorerSort


def _non_empty_string(value: object, *, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > MAX_EXPLORER_VALUE_CHARS:
        raise ValueError(
            f"{path} must contain at most {MAX_EXPLORER_VALUE_CHARS} characters"
        )
    return normalized


def _normalize_scalar_value(field: str, value: object, *, path: str) -> str:
    normalized = _non_empty_string(value, path=path)
    if field == "entity_type" and normalized not in _ENTITY_TYPES:
        raise ValueError(f"{path} must be one of app, agent, run, user")
    return normalized


def _normalize_in_value(field: str, value: object, *, path: str) -> tuple[str, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or not value
    ):
        raise ValueError(f"{path} must be a non-empty list")
    if len(value) > MAX_EXPLORER_IN_VALUES:
        raise ValueError(
            f"{path} must contain at most {MAX_EXPLORER_IN_VALUES} items"
        )
    return tuple(
        _normalize_scalar_value(field, item, path=f"{path}[{index}]")
        for index, item in enumerate(value)
    )


def _normalize_metadata_value(value: object, *, path: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be an object")
    if "key" not in value:
        raise ValueError(f"{path}.key is required")
    if "value" not in value:
        raise ValueError(f"{path}.value is required")
    return {
        "key": _non_empty_string(value["key"], path=f"{path}.key"),
        "value": _non_empty_string(value["value"], path=f"{path}.value"),
    }


def _parse_filter(
    value: object,
    *,
    index: int,
    allowed_fields: set[str],
) -> ExplorerFilter:
    path = f"filters[{index}]"
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be an object")

    field = value.get("field")
    if not isinstance(field, str) or field not in allowed_fields:
        raise ValueError(f"{path}.field is not allowed")

    operator = value.get("operator")
    if not isinstance(operator, str) or operator not in _KNOWN_OPERATORS:
        raise ValueError(f"{path}.operator is invalid")
    compatible_operators = _FIELD_OPERATORS.get(field, _SCALAR_OPERATORS)
    if operator not in compatible_operators:
        raise ValueError(f"{path}.operator is incompatible with {field}")

    raw_value = value.get("value")
    value_path = f"{path}.value"
    if field == "metadata":
        normalized_value: object = _normalize_metadata_value(
            raw_value, path=value_path
        )
    elif operator == "in":
        normalized_value = _normalize_in_value(field, raw_value, path=value_path)
    else:
        normalized_value = _normalize_scalar_value(
            field, raw_value, path=value_path
        )

    return ExplorerFilter(field=field, operator=operator, value=normalized_value)


def _parse_datetime(value: object, *, path: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{path} must be an ISO 8601 datetime")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{path} must be an ISO 8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{path} must include a timezone")
    return parsed


def _parse_date_range(value: object) -> ExplorerDateRange:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ValueError("date_range must be an object")
    from_at = _parse_datetime(value.get("from"), path="date_range.from")
    to_at = _parse_datetime(value.get("to"), path="date_range.to")
    if from_at is not None and to_at is not None and from_at > to_at:
        raise ValueError("date_range.from must not be after date_range.to")
    return ExplorerDateRange(from_at=from_at, to_at=to_at)


def _parse_integer(
    value: object,
    *,
    path: str,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path} must be an integer")
    if value < minimum:
        if maximum is None:
            raise ValueError(f"{path} must be at least {minimum}")
        raise ValueError(f"{path} must be between {minimum} and {maximum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{path} must be between {minimum} and {maximum}")
    return value


def parse_explorer_query(
    payload: Mapping[str, object],
    *,
    allowed_fields: set[str],
) -> ExplorerQuery:
    match = payload.get("match", "all")
    if match not in ("all", "any"):
        raise ValueError("match must be 'all' or 'any'")

    raw_filters = payload.get("filters", [])
    if not isinstance(raw_filters, list):
        raise ValueError("filters must be a list")
    if len(raw_filters) > MAX_EXPLORER_FILTERS:
        raise ValueError(
            f"filters must contain at most {MAX_EXPLORER_FILTERS} items"
        )
    filters = tuple(
        _parse_filter(value, index=index, allowed_fields=allowed_fields)
        for index, value in enumerate(raw_filters)
    )

    date_range = _parse_date_range(payload.get("date_range"))
    page = _parse_integer(payload.get("page", 1), path="page", minimum=1)
    page_size = _parse_integer(
        payload.get("page_size", 20),
        path="page_size",
        minimum=1,
        maximum=100,
    )
    if page * page_size > EXPLORER_RECORD_HORIZON:
        raise ValueError(
            f"page window must not exceed {EXPLORER_RECORD_HORIZON} records"
        )

    sort = payload.get("sort", "created_at_desc")
    if not isinstance(sort, str) or sort not in _SORTS:
        raise ValueError("sort is invalid")

    return ExplorerQuery(
        match=match,
        filters=filters,
        date_range=date_range,
        page=page,
        page_size=page_size,
        sort=sort,
    )
