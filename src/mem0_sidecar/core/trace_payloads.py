import json
import math
from collections.abc import Mapping
from typing import Any

_MAX_STRING_BYTES = 4096
_MAX_ARRAY_ITEMS = 50
_MAX_OBJECT_FIELDS = 50
_MAX_DEPTH = 8
_MAX_PREVIEWS = 20

_TRUNCATION_MARKER = "...[TRUNCATED]"
_REDACTED = "[REDACTED]"
_MAX_DEPTH_MARKER = "[MAX_DEPTH]"
_CIRCULAR_MARKER = "[CIRCULAR]"
_UNPRINTABLE_MARKER = "[UNPRINTABLE]"

_SECRET_KEYS = frozenset(
    {
        "accesstoken",
        "apikey",
        "apisecret",
        "authtoken",
        "authorization",
        "bearertoken",
        "clientpassword",
        "clientsecret",
        "cookie",
        "credential",
        "credentials",
        "csrftoken",
        "encryptionkey",
        "idtoken",
        "password",
        "passwd",
        "privatekey",
        "proxyauthorization",
        "refreshtoken",
        "secret",
        "sessionid",
        "sessiontoken",
        "setcookie",
        "signingkey",
        "token",
        "xapikey",
        "xauthtoken",
        "xcsrftoken",
    }
)

_PREVIEW_FIELDS = frozenset(
    {
        "agent_id",
        "app_id",
        "categories",
        "created_at",
        "expiration_date",
        "id",
        "memory",
        "memory_id",
        "run_id",
        "score",
        "updated_at",
        "user_id",
    }
)


def _bounded_string(value: str) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_STRING_BYTES:
        return encoded.decode("utf-8")

    marker = _TRUNCATION_MARKER.encode("utf-8")
    prefix = encoded[: _MAX_STRING_BYTES - len(marker)]
    return prefix.decode("utf-8", errors="ignore") + _TRUNCATION_MARKER


def _safe_string(value: object) -> str:
    try:
        rendered = str(value)
    except Exception:
        return _UNPRINTABLE_MARKER
    return _bounded_string(rendered)


def _normalized_key(key: str) -> str:
    return key.lower().replace("-", "").replace("_", "")


def _is_secret_key(key: str) -> bool:
    return _normalized_key(key) in _SECRET_KEYS


def _mapping_items(value: Mapping[object, object]) -> list[tuple[str, object]] | None:
    try:
        items = [(_safe_string(key), item) for key, item in value.items()]
    except Exception:
        return None
    return sorted(items, key=lambda pair: pair[0])


def _sanitize(
    value: object,
    *,
    depth: int,
    active_containers: set[int],
) -> dict[str, Any] | list[Any] | str | int | float | bool | None:
    if depth > _MAX_DEPTH:
        return _MAX_DEPTH_MARKER

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        return _bounded_string(value)

    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active_containers:
            return _CIRCULAR_MARKER
        active_containers.add(identity)
        try:
            items = _mapping_items(value)
            if items is None:
                return _UNPRINTABLE_MARKER

            retained = items[:_MAX_OBJECT_FIELDS]
            sanitized: dict[str, Any] = {}
            for key, item in retained:
                sanitized[key] = (
                    _REDACTED
                    if _is_secret_key(key)
                    else _sanitize(
                        item,
                        depth=depth + 1,
                        active_containers=active_containers,
                    )
                )
            if len(items) > _MAX_OBJECT_FIELDS:
                sanitized["_trace_truncated_fields"] = (
                    len(items) - _MAX_OBJECT_FIELDS
                )
            return sanitized
        finally:
            active_containers.remove(identity)

    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active_containers:
            return _CIRCULAR_MARKER
        active_containers.add(identity)
        try:
            sanitized_items = [
                _sanitize(
                    item,
                    depth=depth + 1,
                    active_containers=active_containers,
                )
                for item in value[:_MAX_ARRAY_ITEMS]
            ]
            if len(value) > _MAX_ARRAY_ITEMS:
                sanitized_items.append(
                    {"_trace_truncated_items": len(value) - _MAX_ARRAY_ITEMS}
                )
            return sanitized_items
        finally:
            active_containers.remove(identity)

    return _safe_string(value)


def sanitize_trace_payload(
    value: object,
) -> dict[str, Any] | list[Any] | str | int | float | bool | None:
    """Return a bounded, credential-redacted, JSON-native payload."""

    return _sanitize(value, depth=0, active_containers=set())


def _compact_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def bounded_trace_document(value: object, *, max_bytes: int = 65_536) -> dict[str, Any]:
    """Sanitize a value and return an object within ``max_bytes`` of compact JSON."""

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise TypeError("max_bytes must be an integer")
    if max_bytes < 2:
        raise ValueError("max_bytes must be at least 2 bytes for a JSON object")

    sanitized = sanitize_trace_payload(value)
    document = dict(sanitized) if isinstance(sanitized, dict) else {"value": sanitized}
    original_document_bytes = len(_compact_json_bytes(document))
    if original_document_bytes <= max_bytes:
        return document

    candidates = sorted(
        (
            (key, len(_compact_json_bytes(item)))
            for key, item in document.items()
        ),
        key=lambda candidate: (-candidate[1], candidate[0]),
    )
    for key, original_field_bytes in candidates:
        document[key] = {
            "_trace_truncated": True,
            "original_bytes": original_field_bytes,
        }
        if len(_compact_json_bytes(document)) <= max_bytes:
            return document

    fallback = {
        "_trace_truncated": True,
        "original_bytes": original_document_bytes,
    }
    if len(_compact_json_bytes(fallback)) <= max_bytes:
        return fallback
    return {}


def _trusted_total(value: object, *, result_count: int) -> int | None:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= result_count
    ):
        return value
    return None


def trace_result_summary(
    response: Mapping[str, object],
) -> tuple[int, list[dict[str, Any]]]:
    """Return a safe result count and up to twenty allowlisted memory previews."""

    raw_results = response.get("results")
    results = raw_results if isinstance(raw_results, list) else []
    result_count = len(results)
    trusted_total = _trusted_total(response.get("total"), result_count=result_count)

    previews: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, Mapping):
            continue
        preview = {key: item[key] for key in _PREVIEW_FIELDS if key in item}
        if not preview:
            continue
        sanitized = sanitize_trace_payload(preview)
        if isinstance(sanitized, dict):
            previews.append(sanitized)
        if len(previews) == _MAX_PREVIEWS:
            break

    return trusted_total if trusted_total is not None else result_count, previews
