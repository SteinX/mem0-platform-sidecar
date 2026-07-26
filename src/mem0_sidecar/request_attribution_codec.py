from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypeAlias

RequestTransport: TypeAlias = Literal["mcp", "rest", "system", "unknown"]
CredentialKind: TypeAlias = Literal[
    "core_api_key",
    "legacy_static",
    "operator_static",
    "session",
    "disabled",
    "unknown",
]
JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
_HEADER_KEYS = {
    "v",
    "transport",
    "credential_kind",
    "credential_id",
    "label",
    "key_prefix",
}
_CORE_DESCRIPTOR_KEYS = {"kind", "id", "label", "key_prefix"}
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_HEADER_BYTES = 2048
_TRANSPORT_VALUES: dict[str, RequestTransport] = {
    "mcp": "mcp",
    "rest": "rest",
    "system": "system",
    "unknown": "unknown",
}
_CREDENTIAL_VALUES: dict[str, CredentialKind] = {
    "core_api_key": "core_api_key",
    "legacy_static": "legacy_static",
    "operator_static": "operator_static",
    "session": "session",
    "disabled": "disabled",
    "unknown": "unknown",
}


@dataclass(frozen=True, slots=True)
class AttributionRejected(ValueError):
    reason: str = "Invalid caller attribution."

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class DecodedAttribution:
    transport: RequestTransport
    credential_kind: CredentialKind
    credential_id: str | None
    credential_label: str | None
    credential_prefix: str | None


def _optional_string(value: JSONValue) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise AttributionRejected()


def parse_transport(value: str | None) -> RequestTransport:
    if value is None:
        raise AttributionRejected()
    try:
        return _TRANSPORT_VALUES[value]
    except KeyError as exc:
        raise AttributionRejected() from exc


def parse_credential_kind(value: str | None) -> CredentialKind:
    if value is None:
        raise AttributionRejected()
    try:
        return _CREDENTIAL_VALUES[value]
    except KeyError as exc:
        raise AttributionRejected() from exc


def decode_internal_header(value: str) -> DecodedAttribution:
    try:
        encoded = value.encode("ascii")
    except UnicodeError as exc:
        raise AttributionRejected() from exc
    if (
        not encoded
        or len(encoded) > _MAX_HEADER_BYTES
        or _BASE64URL.fullmatch(value) is None
    ):
        raise AttributionRejected()
    try:
        padding = b"=" * (-len(encoded) % 4)
        decoded = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(decoded.decode("utf-8"))
    except (binascii.Error, UnicodeError, json.JSONDecodeError) as exc:
        raise AttributionRejected() from exc
    if not isinstance(payload, dict) or set(payload) != _HEADER_KEYS:
        raise AttributionRejected()
    if payload.get("v") != 1 or payload.get("transport") != "mcp":
        raise AttributionRejected()
    kind = payload.get("credential_kind")
    if kind not in {"core_api_key", "legacy_static", "disabled"}:
        raise AttributionRejected()
    return DecodedAttribution(
        transport="mcp",
        credential_kind=kind,
        credential_id=_optional_string(payload.get("credential_id")),
        credential_label=_optional_string(payload.get("label")),
        credential_prefix=_optional_string(payload.get("key_prefix")),
    )


def decode_core_descriptor(
    descriptor: Mapping[str, JSONValue],
) -> DecodedAttribution:
    if set(descriptor) != _CORE_DESCRIPTOR_KEYS:
        raise AttributionRejected()
    kind = descriptor.get("kind")
    if not isinstance(kind, str):
        raise AttributionRejected()
    parsed_kind = parse_credential_kind(kind)
    if parsed_kind not in {
        "core_api_key",
        "operator_static",
        "session",
        "disabled",
    }:
        raise AttributionRejected()
    return DecodedAttribution(
        transport="rest",
        credential_kind=parsed_kind,
        credential_id=_optional_string(descriptor.get("id")),
        credential_label=_optional_string(descriptor.get("label")),
        credential_prefix=_optional_string(descriptor.get("key_prefix")),
    )
