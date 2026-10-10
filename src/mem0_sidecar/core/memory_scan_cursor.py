import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import JsonValue

from mem0_sidecar.core.memory_scan_types import (
    JsonObject,
    Keyset,
    MemoryScanRequest,
    MemoryScanValidationError,
)

_CURSOR_MAX_LENGTH = 4096
_CURSOR_VERSION = 1


@dataclass(frozen=True, slots=True)
class CursorScope:
    project_id: str
    app_id: str | None
    project_wide: bool
    user_id: str | None
    agent_id: str | None
    run_id: str | None
    type: str | None
    include_expired: bool


@dataclass(frozen=True, slots=True)
class CursorState:
    snapshot_at: datetime
    upper: Keyset
    after: Keyset
    total: int


def cursor_scope(request: MemoryScanRequest) -> CursorScope:
    return CursorScope(
        project_id=request.project_id,
        app_id=request.app_id,
        project_wide=request.project_wide,
        user_id=request.filters.user_id,
        agent_id=request.filters.agent_id,
        run_id=request.filters.run_id,
        type=request.filters.type,
        include_expired=request.include_expired,
    )


def _scope_payload(scope: CursorScope) -> JsonObject:
    return {
        "project_id": scope.project_id,
        "app_id": scope.app_id,
        "project_wide": scope.project_wide,
        "filters": {
            "user_id": scope.user_id,
            "agent_id": scope.agent_id,
            "run_id": scope.run_id,
            "type": scope.type,
        },
        "include_expired": scope.include_expired,
    }


def _scope_digest(scope: CursorScope) -> str:
    payload = _scope_payload(scope)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _cursor_mac(payload: JsonObject, scope: CursorScope, secret: bytes) -> str:
    signed: JsonObject = {"cursor": payload, "scope": _scope_payload(scope)}
    canonical = json.dumps(signed, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(secret, canonical, hashlib.sha256).hexdigest()


def encode_cursor(state: CursorState, scope: CursorScope, secret: bytes) -> str:
    payload: JsonObject = {
        "v": _CURSOR_VERSION,
        "snapshot_at": state.snapshot_at.isoformat(),
        "upper": [state.upper[0].isoformat(), state.upper[1]],
        "after": [state.after[0].isoformat(), state.after[1]],
        "total": state.total,
        "digest": _scope_digest(scope),
    }
    payload["mac"] = _cursor_mac(payload, scope, secret)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _parse_datetime(value: JsonValue) -> datetime:
    if not isinstance(value, str):
        raise MemoryScanValidationError("invalid memory scan cursor")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise MemoryScanValidationError("invalid memory scan cursor")
        return parsed.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise MemoryScanValidationError("invalid memory scan cursor") from exc


def _parse_keyset(value: JsonValue) -> Keyset:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not isinstance(value[1], str)
        or not value[1]
    ):
        raise MemoryScanValidationError("invalid memory scan cursor")
    return _parse_datetime(value[0]), value[1]


def decode_cursor(token: str, scope: CursorScope, secret: bytes) -> CursorState:
    if not token or len(token) > _CURSOR_MAX_LENGTH:
        raise MemoryScanValidationError("invalid memory scan cursor")
    try:
        padded = token + "=" * (-len(token) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
        value = json.loads(decoded.decode("utf-8"))
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise MemoryScanValidationError("invalid memory scan cursor") from exc
    if not isinstance(value, dict) or set(value) != {
        "v",
        "snapshot_at",
        "upper",
        "after",
        "total",
        "digest",
        "mac",
    }:
        raise MemoryScanValidationError("invalid memory scan cursor")
    mac = value.pop("mac")
    if (
        not isinstance(mac, str)
        or len(mac) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in mac)
    ):
        raise MemoryScanValidationError("invalid memory scan cursor")
    expected_mac = _cursor_mac(value, scope, secret)
    if not hmac.compare_digest(mac, expected_mac):
        if value.get("digest") != _scope_digest(scope):
            raise MemoryScanValidationError(
                "memory scan cursor does not match request scope"
            )
        raise MemoryScanValidationError("invalid memory scan cursor")
    if type(value["v"]) is not int or value["v"] != _CURSOR_VERSION:
        raise MemoryScanValidationError("invalid memory scan cursor")
    if type(value["total"]) is not int or value["total"] < 0:
        raise MemoryScanValidationError("invalid memory scan cursor")
    if not isinstance(value["digest"], str):
        raise MemoryScanValidationError("invalid memory scan cursor")
    cursor = CursorState(
        snapshot_at=_parse_datetime(value["snapshot_at"]),
        upper=_parse_keyset(value["upper"]),
        after=_parse_keyset(value["after"]),
        total=value["total"],
    )
    if cursor.after > cursor.upper:
        raise MemoryScanValidationError("invalid memory scan cursor")
    return cursor
