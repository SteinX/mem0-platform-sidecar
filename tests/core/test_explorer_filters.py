import re
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone

import pytest

from mem0_sidecar.core.explorer_filters import (
    MEMORY_FILTER_FIELDS,
    parse_explorer_query,
)


def test_parse_explorer_query_normalizes_defaults() -> None:
    query = parse_explorer_query({}, allowed_fields=MEMORY_FILTER_FIELDS)

    assert query.match == "all"
    assert query.filters == ()
    assert query.date_range.from_at is None
    assert query.date_range.to_at is None
    assert query.page == 1
    assert query.page_size == 20
    assert query.sort == "created_at_desc"

    with pytest.raises(FrozenInstanceError):
        query.page = 2


def test_parse_explorer_query_normalizes_memory_filter_payload():
    query = parse_explorer_query(
        {
            "match": "all",
            "filters": [
                {"field": "user_id", "operator": "equals", "value": " alice "},
                {
                    "field": "metadata",
                    "operator": "contains",
                    "value": {"key": "source", "value": "codex"},
                },
            ],
            "date_range": {
                "from": "2026-07-01T00:00:00Z",
                "to": "2026-07-13T23:59:59Z",
            },
            "page": 2,
            "page_size": 20,
        },
        allowed_fields=MEMORY_FILTER_FIELDS,
    )

    assert query.match == "all"
    assert query.filters[0].value == "alice"
    assert query.filters[1].value == {"key": "source", "value": "codex"}
    assert query.page == 2
    assert query.page_size == 20


def test_parse_explorer_query_normalizes_in_values_and_iso_dates() -> None:
    query = parse_explorer_query(
        {
            "match": "any",
            "filters": [
                {
                    "field": "entity_type",
                    "operator": "in",
                    "value": [" user ", "agent"],
                },
                {
                    "field": "category",
                    "operator": "not_equals",
                    "value": " archive ",
                },
            ],
            "date_range": {
                "from": "2026-07-01T01:02:03+02:00",
                "to": "2026-07-13T23:59:59Z",
            },
            "sort": "created_at_asc",
        },
        allowed_fields=MEMORY_FILTER_FIELDS,
    )

    assert query.match == "any"
    assert query.filters[0].value == ("user", "agent")
    assert query.filters[1].value == "archive"
    assert query.date_range.from_at == datetime(
        2026, 7, 1, 1, 2, 3, tzinfo=timezone(timedelta(hours=2))
    )
    assert query.date_range.to_at == datetime(2026, 7, 13, 23, 59, 59, tzinfo=UTC)
    assert query.sort == "created_at_asc"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"match": "none"}, "match must be 'all' or 'any'"),
        (
            {
                "filters": [
                    {"field": "user_id", "operator": "starts_with", "value": "a"}
                ]
            },
            "filters[0].operator is invalid",
        ),
        (
            {
                "filters": [
                    {"field": "metadata", "operator": "equals", "value": "source"}
                ]
            },
            "filters[0].operator is incompatible with metadata",
        ),
        (
            {
                "filters": [
                    {"field": "unknown", "operator": "equals", "value": "a"}
                ]
            },
            "filters[0].field is not allowed",
        ),
        (
            {
                "filters": [
                    {"field": "user_id", "operator": "equals", "value": "  "}
                ]
            },
            "filters[0].value must be a non-empty string",
        ),
        (
            {
                "filters": [
                    {"field": "entity_type", "operator": "equals", "value": "team"}
                ]
            },
            "filters[0].value must be one of app, agent, run, user",
        ),
        (
            {
                "filters": [
                    {"field": "user_id", "operator": "in", "value": []}
                ]
            },
            "filters[0].value must be a non-empty list",
        ),
        (
            {
                "filters": [
                    {"field": "user_id", "operator": "in", "value": ["alice", " "]}
                ]
            },
            "filters[0].value[1] must be a non-empty string",
        ),
        (
            {
                "filters": [
                    {
                        "field": "metadata",
                        "operator": "contains",
                        "value": {"value": "codex"},
                    }
                ]
            },
            "filters[0].value.key is required",
        ),
        (
            {
                "filters": [
                    {
                        "field": "metadata",
                        "operator": "contains",
                        "value": {"key": "source", "value": " "},
                    }
                ]
            },
            "filters[0].value.value must be a non-empty string",
        ),
    ],
)
def test_parse_explorer_query_rejects_invalid_filters(
    payload: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        parse_explorer_query(payload, allowed_fields=MEMORY_FILTER_FIELDS)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"date_range": {"from": "not-a-date"}},
            "date_range.from must be an ISO 8601 datetime",
        ),
        (
            {"date_range": {"from": "2026-07-01T00:00:00"}},
            "date_range.from must include a timezone",
        ),
        (
            {
                "date_range": {
                    "from": "2026-07-14T00:00:00Z",
                    "to": "2026-07-13T23:59:59Z",
                }
            },
            "date_range.from must not be after date_range.to",
        ),
        ({"page": 0}, "page must be at least 1"),
        ({"page": True}, "page must be an integer"),
        ({"page_size": 0}, "page_size must be between 1 and 100"),
        ({"page_size": 101}, "page_size must be between 1 and 100"),
        ({"sort": "updated_at_desc"}, "sort is invalid"),
        ({"sort": []}, "sort is invalid"),
    ],
)
def test_parse_explorer_query_rejects_invalid_ranges_and_paging(
    payload: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        parse_explorer_query(payload, allowed_fields=MEMORY_FILTER_FIELDS)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "filters": [
                    {"field": "user_id", "operator": "equals", "value": "a"}
                    for _index in range(65)
                ]
            },
            "filters must contain at most 64 items",
        ),
        (
            {
                "filters": [
                    {
                        "field": "user_id",
                        "operator": "in",
                        "value": [f"user-{index}" for index in range(101)],
                    }
                ]
            },
            "filters[0].value must contain at most 100 items",
        ),
        (
            {
                "filters": [
                    {
                        "field": "user_id",
                        "operator": "equals",
                        "value": "x" * 257,
                    }
                ]
            },
            "filters[0].value must contain at most 256 characters",
        ),
        (
            {
                "filters": [
                    {
                        "field": "metadata",
                        "operator": "contains",
                        "value": {"key": "k" * 257, "value": "v"},
                    }
                ]
            },
            "filters[0].value.key must contain at most 256 characters",
        ),
        (
            {"page": 51, "page_size": 100},
            "page window must not exceed 5000 records",
        ),
    ],
)
def test_parse_explorer_query_rejects_hostile_bounded_work(
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        parse_explorer_query(payload, allowed_fields=MEMORY_FILTER_FIELDS)


def test_parse_explorer_query_accepts_exact_work_bounds() -> None:
    query = parse_explorer_query(
        {
            "filters": [
                {
                    "field": "user_id",
                    "operator": "in",
                    "value": [f"user-{index}" for index in range(100)],
                }
                for _index in range(64)
            ],
            "page": 50,
            "page_size": 100,
        },
        allowed_fields=MEMORY_FILTER_FIELDS,
    )

    assert len(query.filters) == 64
    assert query.page * query.page_size == 5000
