from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TypedDict, assert_never

from mem0_sidecar.request_attribution_codec import (
    AttributionRejected,
    CredentialKind,
    JSONValue,
    RequestTransport,
    decode_core_descriptor,
    decode_internal_header,
    parse_credential_kind,
    parse_transport,
)

CALLER_CONTEXT_HEADER = "X-Mem0-Caller-Context"


class PublicChannel(TypedDict):
    transport: RequestTransport
    credential_kind: CredentialKind
    credential_id: str | None
    label: str
    key_prefix: str | None


def _valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class RequestAttribution:
    transport: RequestTransport
    credential_kind: CredentialKind
    credential_id: str | None = None
    credential_label: str | None = None
    credential_prefix: str | None = None

    def __post_init__(self) -> None:
        no_key_fields = self.credential_id is None and self.credential_prefix is None
        match self.credential_kind:
            case "core_api_key":
                if (
                    self.transport not in {"mcp", "rest"}
                    or self.credential_id is None
                    or not _valid_uuid(self.credential_id)
                    or self.credential_label is None
                    or not 1 <= len(self.credential_label) <= 255
                    or self.credential_prefix is None
                    or not 1 <= len(self.credential_prefix) <= 12
                ):
                    raise AttributionRejected()
            case "legacy_static":
                if (
                    self.transport != "mcp"
                    or not no_key_fields
                    or self.credential_label != "Legacy shared MCP key"
                ):
                    raise AttributionRejected()
            case "operator_static":
                if (
                    self.transport != "rest"
                    or not no_key_fields
                    or self.credential_label != "Legacy admin API key"
                ):
                    raise AttributionRejected()
            case "session":
                if (
                    self.transport != "rest"
                    or not no_key_fields
                    or self.credential_label is not None
                ):
                    raise AttributionRejected()
            case "disabled":
                if (
                    self.transport not in {"mcp", "rest"}
                    or not no_key_fields
                    or self.credential_label is not None
                ):
                    raise AttributionRejected()
            case "unknown":
                if (
                    self.transport not in {"system", "unknown"}
                    or not no_key_fields
                    or self.credential_label is not None
                ):
                    raise AttributionRejected()
            case unreachable:
                assert_never(unreachable)

    @classmethod
    def system(cls) -> RequestAttribution:
        return cls(transport="system", credential_kind="unknown")

    @classmethod
    def historical(cls) -> RequestAttribution:
        return cls(transport="unknown", credential_kind="unknown")

    @classmethod
    def from_internal_header(cls, value: str) -> RequestAttribution:
        decoded = decode_internal_header(value)
        return cls(
            transport=decoded.transport,
            credential_kind=decoded.credential_kind,
            credential_id=decoded.credential_id,
            credential_label=decoded.credential_label,
            credential_prefix=decoded.credential_prefix,
        )

    @classmethod
    def from_core_descriptor(
        cls,
        descriptor: Mapping[str, JSONValue],
    ) -> RequestAttribution:
        decoded = decode_core_descriptor(descriptor)
        return cls(
            transport=decoded.transport,
            credential_kind=decoded.credential_kind,
            credential_id=decoded.credential_id,
            credential_label=decoded.credential_label,
            credential_prefix=decoded.credential_prefix,
        )

    @classmethod
    def from_stored(
        cls,
        *,
        transport: str | None,
        credential_kind: str | None,
        credential_id: str | None,
        credential_label: str | None,
        credential_prefix: str | None,
    ) -> RequestAttribution:
        if transport is None and credential_kind is None:
            return cls.historical()
        try:
            return cls(
                transport=parse_transport(transport),
                credential_kind=parse_credential_kind(credential_kind),
                credential_id=credential_id,
                credential_label=credential_label,
                credential_prefix=credential_prefix,
            )
        except AttributionRejected:
            return cls.historical()

    def to_channel_dict(self) -> PublicChannel:
        match self.credential_kind:
            case "core_api_key" | "legacy_static" | "operator_static":
                label = self.credential_label or "Unknown"
            case "session":
                label = "Authenticated session"
            case "disabled":
                label = "Authentication disabled"
            case "unknown":
                label = (
                    "System"
                    if self.transport == "system"
                    else "Unknown (pre-attribution)"
                )
            case unreachable:
                assert_never(unreachable)
        return {
            "transport": self.transport,
            "credential_kind": self.credential_kind,
            "credential_id": self.credential_id,
            "label": label,
            "key_prefix": self.credential_prefix,
        }


_CURRENT_ATTRIBUTION: ContextVar[RequestAttribution | None] = ContextVar(
    "mem0_request_attribution",
    default=None,
)


@contextmanager
def bind_request_attribution(
    attribution: RequestAttribution,
) -> Iterator[None]:
    token = _CURRENT_ATTRIBUTION.set(attribution)
    try:
        yield
    finally:
        _CURRENT_ATTRIBUTION.reset(token)


def current_request_attribution() -> RequestAttribution:
    return _CURRENT_ATTRIBUTION.get() or RequestAttribution.system()
