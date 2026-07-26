import base64
import json
import threading

import pytest

from mem0_sidecar.request_attribution import (
    AttributionRejected,
    JSONValue,
    RequestAttribution,
    bind_request_attribution,
    current_request_attribution,
)


def _header(payload: dict[str, JSONValue]) -> str:
    serialized = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(serialized).rstrip(b"=").decode("ascii")


def _core_payload(label: str = "codex-devbox") -> dict[str, JSONValue]:
    return {
        "v": 1,
        "transport": "mcp",
        "credential_kind": "core_api_key",
        "credential_id": "e0544e3c-d217-40d9-bc9a-c1f64077542a",
        "label": label,
        "key_prefix": "m0sk_client_",
    }


def test_internal_header_parses_exact_safe_core_key_descriptor() -> None:
    attribution = RequestAttribution.from_internal_header(_header(_core_payload()))

    assert attribution == RequestAttribution(
        transport="mcp",
        credential_kind="core_api_key",
        credential_id="e0544e3c-d217-40d9-bc9a-c1f64077542a",
        credential_label="codex-devbox",
        credential_prefix="m0sk_client_",
    )
    assert attribution.to_channel_dict() == {
        "transport": "mcp",
        "credential_kind": "core_api_key",
        "credential_id": "e0544e3c-d217-40d9-bc9a-c1f64077542a",
        "label": "codex-devbox",
        "key_prefix": "m0sk_client_",
    }


def test_internal_header_parses_only_the_constant_legacy_descriptor() -> None:
    attribution = RequestAttribution.from_internal_header(
        _header(
            {
                "v": 1,
                "transport": "mcp",
                "credential_kind": "legacy_static",
                "credential_id": None,
                "label": "Legacy shared MCP key",
                "key_prefix": None,
            }
        )
    )

    assert attribution.credential_kind == "legacy_static"
    assert attribution.credential_id is None
    assert attribution.credential_prefix is None


@pytest.mark.parametrize(
    "payload",
    [
        {**_core_payload(), "transport": "rest"},
        {**_core_payload(), "credential_kind": "operator_static"},
        {**_core_payload(), "credential_id": "not-a-uuid"},
        {**_core_payload(), "label": ""},
        {**_core_payload(), "label": "x" * 256},
        {**_core_payload(), "key_prefix": "x" * 13},
        {**_core_payload(), "token": "must-not-be-accepted"},
        {
            "v": 1,
            "transport": "mcp",
            "credential_kind": "legacy_static",
            "credential_id": None,
            "label": "forged legacy label",
            "key_prefix": None,
        },
    ],
)
def test_internal_header_rejects_malformed_or_expanded_context(
    payload: dict[str, JSONValue],
) -> None:
    with pytest.raises(AttributionRejected):
        RequestAttribution.from_internal_header(_header(payload))


def test_internal_header_rejects_oversized_and_invalid_base64() -> None:
    with pytest.raises(AttributionRejected):
        RequestAttribution.from_internal_header("x" * 2049)
    with pytest.raises(AttributionRejected):
        RequestAttribution.from_internal_header("not+base64")


def test_core_descriptor_maps_direct_session_and_exact_api_key() -> None:
    session = RequestAttribution.from_core_descriptor(
        {
            "kind": "session",
            "id": None,
            "label": None,
            "key_prefix": None,
        }
    )
    api_key = RequestAttribution.from_core_descriptor(
        {
            "kind": "core_api_key",
            "id": "e0544e3c-d217-40d9-bc9a-c1f64077542a",
            "label": "opencode-devbox",
            "key_prefix": "m0sk_client_",
        }
    )

    assert session == RequestAttribution(
        transport="rest",
        credential_kind="session",
    )
    assert api_key.credential_label == "opencode-devbox"
    assert api_key.transport == "rest"


def test_historical_nulls_and_system_work_are_distinct() -> None:
    historical = RequestAttribution.from_stored(
        transport=None,
        credential_kind=None,
        credential_id=None,
        credential_label=None,
        credential_prefix=None,
    )
    system = RequestAttribution.system()

    assert historical.to_channel_dict()["label"] == "Unknown (pre-attribution)"
    assert historical.transport == "unknown"
    assert system.to_channel_dict()["label"] == "System"
    assert system.transport == "system"


def test_request_context_resets_and_is_thread_local() -> None:
    first = RequestAttribution.from_internal_header(_header(_core_payload("codex")))
    second = RequestAttribution.from_internal_header(_header(_core_payload("opencode")))
    barrier = threading.Barrier(2)
    observed: dict[str, str | None] = {}

    def capture(name: str, attribution: RequestAttribution) -> None:
        with bind_request_attribution(attribution):
            barrier.wait(timeout=5)
            observed[name] = current_request_attribution().credential_label

    threads = [
        threading.Thread(target=capture, args=("first", first)),
        threading.Thread(target=capture, args=("second", second)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert observed == {"first": "codex", "second": "opencode"}
    assert current_request_attribution() == RequestAttribution.system()
