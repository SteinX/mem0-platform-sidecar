import json
from copy import deepcopy

import pytest

from mem0_sidecar.core.trace_payloads import (
    bounded_trace_document,
    sanitize_trace_payload,
    trace_result_summary,
)


def _compact_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def test_sanitize_trace_payload_redacts_nested_secrets():
    assert sanitize_trace_payload(
        {
            "Authorization": "Bearer secret",
            "nested": {"apiKey": "m0-key", "safe": "ok"},
            "headers": [{"Set-Cookie": "session=x"}],
        }
    ) == {
        "Authorization": "[REDACTED]",
        "nested": {"apiKey": "[REDACTED]", "safe": "ok"},
        "headers": [{"Set-Cookie": "[REDACTED]"}],
    }


def test_sanitize_trace_payload_normalizes_only_secret_key_names() -> None:
    payload = {
        "cookie": "cookie-value",
        "access_token": "access-value",
        "refresh-token": "refresh-value",
        "PASSWORD": "password-value",
        "client_secret": "client-value",
        "x-api-key": "api-value",
        "action": "search",
        "secretary": "person",
        "token_count": 12,
    }

    sanitized = sanitize_trace_payload(payload)

    assert sanitized == {
        "PASSWORD": "[REDACTED]",
        "access_token": "[REDACTED]",
        "action": "search",
        "client_secret": "[REDACTED]",
        "cookie": "[REDACTED]",
        "refresh-token": "[REDACTED]",
        "secretary": "person",
        "token_count": 12,
        "x-api-key": "[REDACTED]",
    }


def test_sanitize_trace_payload_bounds_unicode_strings_by_utf8_bytes() -> None:
    sanitized = sanitize_trace_payload("界" * 4096)

    assert isinstance(sanitized, str)
    assert sanitized.endswith("...[TRUNCATED]")
    assert len(sanitized.encode("utf-8")) <= 4096


def test_sanitize_trace_payload_limits_arrays() -> None:
    sanitized = sanitize_trace_payload(list(range(55)))

    assert sanitized == [
        *range(50),
        {"_trace_truncated_items": 5},
    ]


def test_sanitize_trace_payload_limits_object_fields_deterministically() -> None:
    ascending = {f"field-{index:02d}": index for index in range(55)}
    descending = dict(reversed(list(ascending.items())))

    sanitized_ascending = sanitize_trace_payload(ascending)
    sanitized_descending = sanitize_trace_payload(descending)

    assert sanitized_ascending == sanitized_descending
    assert isinstance(sanitized_ascending, dict)
    assert sanitized_ascending["_trace_truncated_fields"] == 5
    assert "field-00" in sanitized_ascending
    assert "field-49" in sanitized_ascending
    assert "field-50" not in sanitized_ascending


def test_sanitize_trace_payload_stops_at_depth_limit_and_handles_cycles() -> None:
    payload: dict[str, object] = {}
    cursor = payload
    for _ in range(10):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child

    cyclic: list[object] = []
    cyclic.append(cyclic)

    sanitized = sanitize_trace_payload({"deep": payload, "cyclic": cyclic})

    assert "[MAX_DEPTH]" in json.dumps(sanitized)
    assert isinstance(sanitized, dict)
    assert sanitized["cyclic"] == ["[CIRCULAR]"]


def test_sanitize_trace_payload_bounds_and_protects_unknown_object_strings() -> None:
    class LongString:
        def __str__(self) -> str:
            return "界" * 4096

    class BrokenString:
        def __str__(self) -> str:
            raise RuntimeError("cannot stringify")

    sanitized = sanitize_trace_payload([LongString(), BrokenString()])

    assert isinstance(sanitized, list)
    assert isinstance(sanitized[0], str)
    assert sanitized[0].endswith("...[TRUNCATED]")
    assert len(sanitized[0].encode("utf-8")) <= 4096
    assert sanitized[1] == "[UNPRINTABLE]"


def test_sanitize_trace_payload_produces_strict_json_native_values() -> None:
    sanitized = sanitize_trace_payload(
        {
            "finite": 1.5,
            "nan": float("nan"),
            "positive_infinity": float("inf"),
            "negative_infinity": float("-inf"),
            "tuple": (1, True, None),
        }
    )

    assert sanitized == {
        "finite": 1.5,
        "nan": None,
        "negative_infinity": None,
        "positive_infinity": None,
        "tuple": [1, True, None],
    }
    assert json.loads(_compact_bytes(sanitized)) == sanitized


def test_bounded_trace_document_envelopes_non_object_roots() -> None:
    assert bounded_trace_document(7) == {"value": 7}
    assert bounded_trace_document(["one", "two"]) == {"value": ["one", "two"]}


def test_bounded_trace_document_is_deterministic_and_within_default_limit() -> None:
    payload = {f"field-{index:02d}": "界" * 4096 for index in range(20)}

    first = bounded_trace_document(payload)
    second = bounded_trace_document(dict(reversed(list(payload.items()))))

    assert first == second
    assert len(_compact_bytes(first)) <= 65_536
    assert any(
        isinstance(value, dict) and value.get("_trace_truncated") is True
        for value in first.values()
    )


def test_bounded_trace_document_replaces_largest_fields_with_stable_ties() -> None:
    payload = {
        "charlie": "c" * 4000,
        "bravo": "b" * 4000,
        "alpha": "a" * 4000,
    }
    sanitized = sanitize_trace_payload(payload)

    bounded = bounded_trace_document(payload, max_bytes=8500)

    assert isinstance(sanitized, dict)
    assert bounded["alpha"] == {
        "_trace_truncated": True,
        "original_bytes": len(_compact_bytes(sanitized["alpha"])),
    }
    assert bounded["bravo"] == "b" * 4000
    assert bounded["charlie"] == "c" * 4000
    assert len(_compact_bytes(bounded)) <= 8500


def test_bounded_trace_document_has_defined_minimum_size_behavior() -> None:
    assert bounded_trace_document({"large": "x" * 100}, max_bytes=2) == {}
    assert len(_compact_bytes(bounded_trace_document("界" * 100, max_bytes=10))) <= 10

    with pytest.raises(ValueError, match="at least 2"):
        bounded_trace_document({}, max_bytes=1)


def test_trace_payload_functions_do_not_modify_the_input() -> None:
    payload = {
        "Authorization": "secret",
        "nested": [*range(55)],
        "large": "x" * 4096,
    }
    original = deepcopy(payload)

    sanitize_trace_payload(payload)
    bounded_trace_document(payload, max_bytes=100)

    assert payload == original


def test_trace_result_summary_limits_previews():
    count, previews = trace_result_summary(
        {"results": [{"id": f"mem-{index}", "memory": "x"} for index in range(25)]}
    )
    assert count == 25
    assert len(previews) == 20


def test_trace_result_summary_uses_a_strict_sanitized_allowlist() -> None:
    count, previews = trace_result_summary(
        {
            "total": 30,
            "results": [
                {
                    "id": "mem-1",
                    "memory_id": "legacy-1",
                    "memory": "Remember me",
                    "user_id": "user-1",
                    "agent_id": "agent-1",
                    "app_id": "app-1",
                    "run_id": "run-1",
                    "categories": [
                        "decision",
                        {"name": "safe", "api_key": "hidden"},
                    ],
                    "score": float("nan"),
                    "created_at": "2026-07-13T00:00:00Z",
                    "updated_at": "2026-07-13T00:01:00Z",
                    "expiration_date": None,
                    "metadata": {"safe": "must not persist"},
                    "Authorization": "Bearer credential",
                    "password": "credential",
                    "action": "unknown field",
                }
            ],
        }
    )

    assert count == 30
    assert previews == [
        {
            "agent_id": "agent-1",
            "app_id": "app-1",
            "categories": [
                "decision",
                {"api_key": "[REDACTED]", "name": "safe"},
            ],
            "created_at": "2026-07-13T00:00:00Z",
            "expiration_date": None,
            "id": "mem-1",
            "memory": "Remember me",
            "memory_id": "legacy-1",
            "run_id": "run-1",
            "score": None,
            "updated_at": "2026-07-13T00:01:00Z",
            "user_id": "user-1",
        }
    ]


def test_trace_result_summary_handles_malformed_results_and_totals() -> None:
    assert trace_result_summary({"results": "malformed", "total": 7}) == (7, [])

    count, previews = trace_result_summary(
        {"results": [None, "bad", {"id": "mem-1"}], "total": 1}
    )
    assert count == 3
    assert previews == [{"id": "mem-1"}]

    assert trace_result_summary({"results": [{"id": "mem-1"}], "total": True}) == (
        1,
        [{"id": "mem-1"}],
    )
    assert trace_result_summary({}) == (0, [])
