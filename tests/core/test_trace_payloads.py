import json
from collections import UserList, deque
from collections.abc import Iterator, Mapping, Sequence
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


def test_sanitize_trace_payload_redacts_compound_secret_keys_without_visiting_values(
) -> None:
    class SecretValue:
        def __str__(self) -> str:
            raise AssertionError("secret values must never be stringified")

    secret = SecretValue()
    payload = {
        "api_token": secret,
        "secret_key": secret,
        "openai_api_key": secret,
        "mem0_api_key": secret,
        "aws_secret_access_key": secret,
        "aws_access_key_id": secret,
        "client_secret": secret,
        "action": "keep",
        "token_count": 2,
        "secret_count": 3,
        "monkey": "banana",
    }

    sanitized = sanitize_trace_payload(payload)

    assert sanitized == {
        "action": "keep",
        "api_token": "[REDACTED]",
        "aws_access_key_id": "[REDACTED]",
        "aws_secret_access_key": "[REDACTED]",
        "client_secret": "[REDACTED]",
        "mem0_api_key": "[REDACTED]",
        "monkey": "banana",
        "openai_api_key": "[REDACTED]",
        "secret_count": 3,
        "secret_key": "[REDACTED]",
        "token_count": 2,
    }


def test_sanitize_trace_payload_detects_secret_suffix_on_a_truncated_key() -> None:
    class SecretValue:
        def __str__(self) -> str:
            raise AssertionError("secret values must never be stringified")

    sanitized = sanitize_trace_payload({("x" * 100_000) + "_api_key": SecretValue()})

    assert isinstance(sanitized, dict)
    assert list(sanitized.values()) == ["[REDACTED]"]
    assert len(next(iter(sanitized)).encode("utf-8")) <= 4096


def test_sanitize_trace_payload_uses_credential_aware_key_segments() -> None:
    class SecretMapping(Mapping[str, object]):
        def __init__(self) -> None:
            self.keys = [
                "GitHubToken",
                "gitlab-private-token",
                "slackBotToken",
                "Authorization-Code",
                "codeVerifier",
                "PASSPHRASE",
            ]
            self.value_reads = 0

        def __iter__(self) -> Iterator[str]:
            return iter(self.keys)

        def __len__(self) -> int:
            return len(self.keys)

        def __getitem__(self, key: str) -> object:
            self.value_reads += 1
            raise AssertionError(f"credential value was read for {key}")

    payload = SecretMapping()

    sanitized = sanitize_trace_payload(payload)

    assert sanitized == {key: "[REDACTED]" for key in sorted(payload.keys)}
    assert payload.value_reads == 0


def test_sanitize_trace_payload_defaults_token_and_plural_credentials_to_secret(
) -> None:
    class SecretMapping(Mapping[str, object]):
        def __init__(self) -> None:
            self.keys = [
                "huggingface_token",
                "hf-token",
                "npmToken",
                "pypiToken",
                "notion_token",
                "api_keys",
                "tokens",
                "secrets",
                "passwords",
                "cookies",
                "credentials",
            ]
            self.value_reads = 0

        def __iter__(self) -> Iterator[str]:
            return iter(self.keys)

        def __len__(self) -> int:
            return len(self.keys)

        def __getitem__(self, key: str) -> object:
            self.value_reads += 1
            raise AssertionError(f"credential value was read for {key}")

    payload = SecretMapping()

    sanitized = sanitize_trace_payload(payload)

    assert sanitized == {key: "[REDACTED]" for key in sorted(payload.keys)}
    assert payload.value_reads == 0


def test_sanitize_trace_payload_normalizes_collapsed_credential_suffixes() -> None:
    class SecretMapping(Mapping[str, object]):
        def __init__(self) -> None:
            self.keys = [
                "OpenAIAPIKey",
                "OPENAIAPIKEY",
                "openaiapikey",
                "awssecretaccesskey",
                "mem0apikey",
            ]
            self.value_reads = 0

        def __iter__(self) -> Iterator[str]:
            return iter(self.keys)

        def __len__(self) -> int:
            return len(self.keys)

        def __getitem__(self, key: str) -> object:
            self.value_reads += 1
            raise AssertionError(f"credential value was read for {key}")

    payload = SecretMapping()

    sanitized = sanitize_trace_payload(payload)

    assert sanitized == {key: "[REDACTED]" for key in sorted(payload.keys)}
    assert payload.value_reads == 0


def test_sanitize_trace_payload_redacts_compound_passwd_session_and_access_keys(
) -> None:
    class SecretMapping(Mapping[str, object]):
        def __init__(self) -> None:
            self.keys = [
                "db_passwd",
                "DBPASSWD",
                "php_session_id",
                "PHPSESSIONID",
                "session_ids",
                "aws_access_key",
                "AWSACCESSKEY",
            ]
            self.value_reads = 0

        def __iter__(self) -> Iterator[str]:
            return iter(self.keys)

        def __len__(self) -> int:
            return len(self.keys)

        def __getitem__(self, key: str) -> object:
            self.value_reads += 1
            raise AssertionError(f"credential value was read for {key}")

    payload = SecretMapping()

    sanitized = sanitize_trace_payload(payload)

    assert sanitized == {key: "[REDACTED]" for key in sorted(payload.keys)}
    assert payload.value_reads == 0


@pytest.mark.parametrize(
    "suffix",
    [
        "api_key",
        "api_keys",
        "access_key",
        "access_keys",
        "access_key_id",
        "access_key_ids",
        "secret_access_key",
        "secret_access_keys",
        "private_key",
        "private_keys",
        "signing_key",
        "signing_keys",
        "encryption_key",
        "encryption_keys",
        "token",
        "tokens",
        "secret",
        "secrets",
        "password",
        "passwords",
        "passwd",
        "passwds",
        "passphrase",
        "passphrases",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "session_id",
        "session_ids",
        "authorization",
        "authorizations",
        "authorization_code",
        "authorization_codes",
        "code_verifier",
        "code_verifiers",
    ],
)
def test_sanitize_trace_payload_keeps_credential_suffix_variants_consistent(
    suffix: str,
) -> None:
    class SecretValue:
        def __str__(self) -> str:
            raise AssertionError("credential value must never be stringified")

    sanitized = sanitize_trace_payload({f"service_{suffix}": SecretValue()})

    assert list(sanitized.values()) == ["[REDACTED]"]


def test_sanitize_trace_payload_preserves_status_and_count_fields() -> None:
    payload = {
        "requires_authorization": False,
        "REQUIRESAUTHORIZATION": False,
        "favorite_cookie": "chocolate-chip",
        "favoriteCookie": "oatmeal",
        "has_password": True,
        "hasPassword": True,
        "is_secret": False,
        "ISSECRET": False,
        "token_count": 2,
        "TOKENCOUNT": 4,
        "secret_count": 3,
        "SECRETCOUNT": 5,
        "action": "search",
        "secretary": "person",
        "monkey": "banana",
    }

    assert sanitize_trace_payload(payload) == payload


def test_sanitize_trace_payload_exempts_unsegmented_state_phrase_suffixes() -> None:
    states = {
        "ACCOUNTHASPASSWORD": True,
        "FEATUREREQUIRESAUTHORIZATION": False,
        "ACCOUNTFAVORITECOOKIE": "oatmeal",
        "ACCOUNTISSECRET": False,
    }

    sanitized = sanitize_trace_payload(
        {
            **states,
            "ACCOUNT_PASSWORD": "credential",
            "redis_secret": "credential",
        }
    )

    assert sanitized == {
        **states,
        "ACCOUNT_PASSWORD": "[REDACTED]",
        "redis_secret": "[REDACTED]",
    }


def test_sanitize_trace_payload_bounds_unicode_strings_by_utf8_bytes() -> None:
    sanitized = sanitize_trace_payload("界" * 4096)

    assert isinstance(sanitized, str)
    assert sanitized.endswith("...[TRUNCATED]")
    assert len(sanitized.encode("utf-8")) <= 4096


def test_sanitize_trace_payload_bypasses_hostile_string_subclass_methods() -> None:
    class HostileString(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:
            raise AssertionError("overridden encode must not run")

        def __len__(self) -> int:
            raise AssertionError("overridden len must not run")

        def __getitem__(self, key: object) -> str:
            raise AssertionError("overridden slicing must not run")

    sanitized = sanitize_trace_payload(HostileString("界" * 1_000_000))

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


def test_sanitize_trace_payload_reads_only_a_fixed_mapping_prefix() -> None:
    class SpyMapping(Mapping[str, int]):
        def __init__(self) -> None:
            self.iterated = 0
            self.read = 0

        def __iter__(self) -> Iterator[str]:
            for index in range(1000):
                self.iterated += 1
                yield f"field-{index:04d}"

        def __len__(self) -> int:
            return 1_000_000

        def __getitem__(self, key: str) -> int:
            self.read += 1
            return int(key.removeprefix("field-"))

    payload = SpyMapping()

    sanitized = sanitize_trace_payload(payload)

    assert sanitized == {"_trace_truncated": True, "original_fields": 1_000_000}
    assert payload.iterated <= 65
    assert payload.read == 0


def test_sanitize_trace_payload_large_mapping_marker_ignores_insertion_order() -> None:
    ascending = {f"field-{index:03d}": index for index in range(80)}
    descending = dict(reversed(list(ascending.items())))

    assert sanitize_trace_payload(ascending) == sanitize_trace_payload(descending) == {
        "_trace_truncated": True,
        "original_fields": 80,
    }


def test_sanitize_trace_payload_has_a_global_traversal_budget() -> None:
    class CountingLeaf:
        def __init__(self) -> None:
            self.calls = 0

        def __str__(self) -> str:
            self.calls += 1
            return "leaf"

    leaf = CountingLeaf()
    payload: object = leaf
    for _ in range(6):
        payload = [payload] * 5

    sanitized = sanitize_trace_payload(payload)

    assert leaf.calls <= 512
    assert "[NODE_LIMIT]" in json.dumps(sanitized)


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


def test_sanitize_trace_payload_bypasses_hostile_sequence_subclass_methods() -> None:
    class HostileList(list[object]):
        def __iter__(self) -> Iterator[object]:
            raise AssertionError("overridden iteration must not run")

        def __len__(self) -> int:
            raise AssertionError("overridden len must not run")

        def __getitem__(self, key: object) -> object:
            raise AssertionError("overridden slicing must not run")

    class HostileTuple(tuple[object, ...]):
        def __iter__(self) -> Iterator[object]:
            raise AssertionError("overridden iteration must not run")

        def __len__(self) -> int:
            raise AssertionError("overridden len must not run")

        def __getitem__(self, key: object) -> object:
            raise AssertionError("overridden slicing must not run")

    assert sanitize_trace_payload(HostileList([1, 2])) == [1, 2]
    assert sanitize_trace_payload(HostileTuple((1, 2))) == [1, 2]


def test_sanitize_trace_payload_bounds_binary_containers_without_stringifying() -> None:
    class HostileBytes(bytes):
        def __str__(self) -> str:
            raise AssertionError("binary data must not be stringified")

        def __len__(self) -> int:
            raise AssertionError("overridden len must not run")

    raw = b"x" * (8 * 1024 * 1024)
    values = [HostileBytes(raw), bytearray(raw), memoryview(raw)]

    sanitized = [sanitize_trace_payload(value) for value in values]

    assert sanitized == [
        "[BINARY:bytes:8388608_BYTES]",
        "[BINARY:bytearray:8388608_BYTES]",
        "[BINARY:memoryview:8388608_BYTES]",
    ]
    assert all(len(_compact_bytes(value)) < 64 for value in sanitized)


def test_sanitize_trace_payload_bounds_common_sequence_containers() -> None:
    class HostileUserList(UserList[object]):
        def __str__(self) -> str:
            raise AssertionError("UserList must not be stringified")

        def __len__(self) -> int:
            raise AssertionError("overridden len must not run")

        def __getitem__(self, index: object) -> object:
            raise AssertionError("overridden indexing must not run")

    class SpySequence(Sequence[object]):
        def __init__(self) -> None:
            self.reads = 0

        def __len__(self) -> int:
            return 8 * 1024 * 1024

        def __getitem__(self, index: int) -> object:
            self.reads += 1
            return index

        def __str__(self) -> str:
            raise AssertionError("Sequence must not be stringified")

    user_list = HostileUserList(range(55))
    values = [user_list, deque(range(55))]
    spy = SpySequence()

    for value in values:
        sanitized = sanitize_trace_payload(value)
        assert isinstance(sanitized, list)
        assert sanitized[:50] == list(range(50))
        assert sanitized[-1] == {"_trace_truncated_items": 5}

    sanitized_spy = sanitize_trace_payload(spy)
    assert isinstance(sanitized_spy, list)
    assert sanitized_spy[:50] == list(range(50))
    assert sanitized_spy[-1] == {"_trace_truncated_items": 8_388_558}
    assert spy.reads == 50


def test_sanitize_trace_payload_namespaces_non_string_keys_and_marks_collisions(
) -> None:
    class SameTextKey:
        def __str__(self) -> str:
            return "same"

    first = SameTextKey()
    second = SameTextKey()
    forward = {first: "first", second: "second"}
    reverse = {second: "second", first: "first"}

    namespaced = sanitize_trace_payload({1: "integer", "1": "string"})
    collided_forward = sanitize_trace_payload(forward)
    collided_reverse = sanitize_trace_payload(reverse)

    assert namespaced == {"1": "string", "[builtins.int]:1": "integer"}
    assert collided_forward == collided_reverse
    assert isinstance(collided_forward, dict)
    assert list(collided_forward.values()) == [{"_trace_key_collision": 2}]


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


def test_sanitize_trace_payload_replaces_huge_integers_with_a_json_safe_marker(
) -> None:
    huge = 1 << 20_000

    sanitized = sanitize_trace_payload({"huge": huge})
    bounded = bounded_trace_document({"huge": huge})

    assert sanitized == {"huge": "[INTEGER_TOO_LARGE:20001_BITS]"}
    assert bounded == sanitized
    assert json.loads(_compact_bytes(bounded)) == bounded


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


def test_bounded_trace_document_rejects_hostile_integer_subclass_limit() -> None:
    class HostileInt(int):
        def __lt__(self, other: object) -> bool:
            raise AssertionError("hostile comparison must not run")

        def __ge__(self, other: object) -> bool:
            raise AssertionError("hostile comparison must not run")

    with pytest.raises(TypeError, match="max_bytes must be an integer"):
        bounded_trace_document({"safe": "value"}, max_bytes=HostileInt(100))


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
            ],
            "created_at": "2026-07-13T00:00:00Z",
            "id": "mem-1",
            "memory": "Remember me",
            "memory_id": "legacy-1",
            "run_id": "run-1",
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


def test_trace_result_summary_enforces_preview_field_types() -> None:
    count, previews = trace_result_summary(
        {
            "results": [
                {
                    "id": 123,
                    "memory_id": "mem-1",
                    "memory": "safe",
                    "user_id": True,
                    "agent_id": "agent-1",
                    "categories": ("one", "two"),
                    "score": 1 << 200,
                    "created_at": None,
                    "updated_at": "2026-07-13T00:00:00Z",
                },
                {
                    "id": "mem-2",
                    "categories": ["safe", {"password": "secret"}],
                    "score": 0.75,
                },
            ]
        }
    )

    assert count == 2
    assert previews == [
        {
            "agent_id": "agent-1",
            "categories": ["one", "two"],
            "memory": "safe",
            "memory_id": "mem-1",
            "updated_at": "2026-07-13T00:00:00Z",
        },
        {"categories": ["safe"], "id": "mem-2", "score": 0.75},
    ]


def test_trace_result_summary_bounds_scanning_and_handles_hostile_mappings() -> None:
    accesses = 0

    class EmptySpyMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            nonlocal accesses
            accesses += 1
            raise KeyError(key)

        def __iter__(self) -> Iterator[str]:
            return iter(())

        def __len__(self) -> int:
            return 0

    class BrokenResponse(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            raise RuntimeError("unreadable")

        def __iter__(self) -> Iterator[str]:
            raise RuntimeError("unreadable")

        def __len__(self) -> int:
            raise RuntimeError("unreadable")

        def get(self, key: str, default: object = None) -> object:
            raise RuntimeError("unreadable")

    class HostileResults(list[object]):
        def __iter__(self) -> Iterator[object]:
            raise AssertionError("overridden iteration must not run")

        def __len__(self) -> int:
            raise AssertionError("overridden len must not run")

        def __getitem__(self, key: object) -> object:
            raise AssertionError("overridden indexing must not run")

    count, previews = trace_result_summary(
        {"results": [EmptySpyMapping() for _ in range(1000)]}
    )

    assert count == 1000
    assert previews == []
    assert accesses <= 1200
    assert trace_result_summary(BrokenResponse()) == (0, [])
    assert trace_result_summary(
        {"results": HostileResults([{"id": "mem-1"}])}
    ) == (1, [{"id": "mem-1"}])
