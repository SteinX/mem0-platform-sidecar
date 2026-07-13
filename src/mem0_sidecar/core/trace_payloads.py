import json
import math
import re
from collections import UserList, deque
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

_MAX_STRING_BYTES = 4096
_MAX_SECRET_KEY_CHARS = 256
_MAX_ARRAY_ITEMS = 50
_MAX_OBJECT_FIELDS = 50
_MAX_MAPPING_SCAN_ITEMS = 64
_MAX_DEPTH = 8
_MAX_TRAVERSAL_NODES = 512
_MAX_INT_BITS = 4096
_MAX_SCORE_INT_BITS = 53
_MAX_TOTAL_INT_BITS = 63
_MAX_PREVIEWS = 20
_MAX_PREVIEW_SCAN_ITEMS = 100

_TRUNCATION_MARKER = "...[TRUNCATED]"
_REDACTED = "[REDACTED]"
_MAX_DEPTH_MARKER = "[MAX_DEPTH]"
_NODE_LIMIT_MARKER = "[NODE_LIMIT]"
_CIRCULAR_MARKER = "[CIRCULAR]"
_UNPRINTABLE_MARKER = "[UNPRINTABLE]"
_UNREADABLE_MARKER = "[UNREADABLE]"
_MISSING = object()

_CAMEL_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_WORD_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_KEY_SEPARATOR = re.compile(r"[^A-Za-z0-9]+")

_NON_CREDENTIAL_KEY_TOKENS = frozenset({"favorite", "has", "is", "require", "requires"})
_NON_CREDENTIAL_TRAILING_TOKENS = frozenset(
    {"configured", "count", "counts", "enabled", "present", "required", "status"}
)
_NON_CREDENTIAL_NORMALIZED_KEYS = frozenset(
    {"favoritecookie", "haspassword", "issecret", "requiresauthorization"}
)
_NON_CREDENTIAL_NORMALIZED_SUFFIXES = (
    "configured",
    "count",
    "counts",
    "enabled",
    "present",
    "required",
    "status",
)
_CREDENTIAL_NORMALIZED_SUFFIXES = (
    "accesskey",
    "accesskeyid",
    "accesskeyids",
    "accesskeys",
    "apikey",
    "apikeys",
    "authorization",
    "authorizations",
    "authorizationcode",
    "authorizationcodes",
    "codeverifier",
    "codeverifiers",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "encryptionkey",
    "encryptionkeys",
    "passphrase",
    "passphrases",
    "passwd",
    "passwds",
    "password",
    "passwords",
    "privatekey",
    "privatekeys",
    "secret",
    "secretaccesskey",
    "secretaccesskeys",
    "secretkey",
    "secretkeys",
    "secrets",
    "sessionid",
    "sessionids",
    "signingkey",
    "signingkeys",
    "token",
    "tokens",
)

_PREVIEW_STRING_FIELDS = (
    "agent_id",
    "app_id",
    "created_at",
    "expiration_date",
    "id",
    "memory",
    "memory_id",
    "run_id",
    "updated_at",
    "user_id",
)


class _TraversalBudget:
    __slots__ = ("remaining",)

    def __init__(self) -> None:
        self.remaining = _MAX_TRAVERSAL_NODES

    def consume(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def _bounded_string(value: str) -> str:
    """Bound a string without encoding more than 4,097 input characters."""

    try:
        character_count = str.__len__(value)
        prefix = str.__getitem__(
            value,
            slice(0, min(character_count, _MAX_STRING_BYTES + 1)),
        )
        encoded = str.encode(prefix, "utf-8", "replace")
    except Exception:
        return _UNPRINTABLE_MARKER

    if character_count <= _MAX_STRING_BYTES and len(encoded) <= _MAX_STRING_BYTES:
        return bytes.decode(encoded, "utf-8")

    marker = str.encode(_TRUNCATION_MARKER, "utf-8")
    bounded_prefix = encoded[: _MAX_STRING_BYTES - len(marker)]
    return bytes.decode(bounded_prefix, "utf-8", "ignore") + _TRUNCATION_MARKER


def _safe_string(value: object) -> str:
    try:
        rendered = str(value)
    except Exception:
        return _UNPRINTABLE_MARKER
    return _bounded_string(rendered)


def _key_tail(key: str) -> str:
    try:
        character_count = str.__len__(key)
        return str.__getitem__(
            key,
            slice(max(character_count - _MAX_SECRET_KEY_CHARS, 0), character_count),
        )
    except Exception:
        return ""


def _key_tokens(key: str) -> tuple[str, ...]:
    tail = _key_tail(key)
    segmented = _CAMEL_ACRONYM_BOUNDARY.sub(r"\1_\2", tail)
    segmented = _CAMEL_WORD_BOUNDARY.sub(r"\1_\2", segmented)
    return tuple(
        token.lower() for token in _KEY_SEPARATOR.split(segmented) if token
    )


def _is_secret_key(key: str) -> bool:
    tokens = _key_tokens(key)
    if not tokens:
        return False
    normalized = "".join(tokens)
    if normalized in _NON_CREDENTIAL_NORMALIZED_KEYS:
        return False
    if tokens[-1] in _NON_CREDENTIAL_TRAILING_TOKENS or normalized.endswith(
        _NON_CREDENTIAL_NORMALIZED_SUFFIXES
    ):
        return False
    if any(token in _NON_CREDENTIAL_KEY_TOKENS for token in tokens[:-1]):
        return False
    return normalized.endswith(_CREDENTIAL_NORMALIZED_SUFFIXES)


def trace_key_is_secret(key: str) -> bool:
    """Return whether a bounded trace key belongs to the secret vocabulary."""

    return _is_secret_key(key)


def _type_namespace(value: object) -> str:
    value_type = type(value)
    try:
        module = type.__getattribute__(value_type, "__module__")
        qualname = type.__getattribute__(value_type, "__qualname__")
    except Exception:
        return "unknown"
    if not isinstance(module, str) or not isinstance(qualname, str):
        return "unknown"
    return _bounded_string(f"{module}.{qualname}")


def _output_key(key: object) -> tuple[str, bool]:
    if isinstance(key, str):
        output = _bounded_string(key)
        return output, _is_secret_key(key)
    output = _bounded_string(f"[{_type_namespace(key)}]:{_safe_string(key)}")
    return output, False


def _mapping_iterator(value: Mapping[object, object]) -> Iterator[object]:
    if isinstance(value, dict):
        return dict.__iter__(value)
    return iter(value)


def _mapping_length(value: Mapping[object, object]) -> int | None:
    try:
        length = dict.__len__(value) if isinstance(value, dict) else len(value)
    except Exception:
        return None
    return length if length >= 0 else None


def _mapping_value(value: Mapping[object, object], key: object) -> object:
    if isinstance(value, dict):
        return dict.__getitem__(value, key)
    return value[key]


def _bounded_mapping_keys(
    value: Mapping[object, object],
) -> tuple[list[object], bool] | None:
    try:
        iterator = _mapping_iterator(value)
    except Exception:
        return None

    keys: list[object] = []
    for slot in range(_MAX_MAPPING_SCAN_ITEMS + 1):
        try:
            key = next(iterator)
        except StopIteration:
            return keys, True
        except Exception:
            return None
        if slot == _MAX_MAPPING_SCAN_ITEMS:
            return keys, False
        keys.append(key)
    return keys, False


def _truncated_field_count(
    *,
    observed_count: int,
    iteration_complete: bool,
    reported_count: int | None,
) -> int | str | None:
    minimum_count = observed_count + (not iteration_complete)
    if reported_count is not None and reported_count >= minimum_count:
        return max(reported_count - _MAX_OBJECT_FIELDS, 0) or None
    if iteration_complete:
        return max(observed_count - _MAX_OBJECT_FIELDS, 0) or None
    omitted_lower_bound = max(minimum_count - _MAX_OBJECT_FIELDS, 1)
    return f">={omitted_lower_bound}"


def _sanitize_mapping(
    value: Mapping[object, object],
    *,
    depth: int,
    active_containers: set[int],
    budget: _TraversalBudget,
) -> dict[str, Any] | str:
    scanned = _bounded_mapping_keys(value)
    if scanned is None:
        return _UNPRINTABLE_MARKER
    raw_keys, iteration_complete = scanned
    if not iteration_complete:
        marker: dict[str, Any] = {"_trace_truncated": True}
        reported_count = _mapping_length(value)
        if reported_count is not None and reported_count > _MAX_MAPPING_SCAN_ITEMS:
            marker["original_fields"] = reported_count
        return marker

    grouped: dict[str, list[tuple[object, bool]]] = {}
    for raw_key in raw_keys:
        output_key, is_secret = _output_key(raw_key)
        grouped.setdefault(output_key, []).append((raw_key, is_secret))

    sanitized: dict[str, Any] = {}
    for output_key in sorted(grouped)[:_MAX_OBJECT_FIELDS]:
        matches = grouped[output_key]
        if len(matches) > 1:
            sanitized[output_key] = {"_trace_key_collision": len(matches)}
            continue

        raw_key, is_secret = matches[0]
        if is_secret:
            sanitized[output_key] = _REDACTED
            continue
        try:
            item = _mapping_value(value, raw_key)
        except Exception:
            sanitized[output_key] = _UNREADABLE_MARKER
            continue
        sanitized[output_key] = _sanitize(
            item,
            depth=depth + 1,
            active_containers=active_containers,
            budget=budget,
        )

    truncated_fields = _truncated_field_count(
        observed_count=len(raw_keys),
        iteration_complete=iteration_complete,
        reported_count=_mapping_length(value),
    )
    if truncated_fields is not None:
        sanitized["_trace_truncated_fields"] = truncated_fields
    return sanitized


def _user_list_data(value: UserList[object]) -> list[object]:
    data = object.__getattribute__(value, "data")
    if not isinstance(data, list):
        raise TypeError("UserList data must be a list")
    return data


def _sequence_length(value: object) -> int:
    if isinstance(value, list):
        return list.__len__(value)
    if isinstance(value, tuple):
        return tuple.__len__(value)
    if isinstance(value, UserList):
        return list.__len__(_user_list_data(value))
    if isinstance(value, deque):
        return deque.__len__(value)
    if isinstance(value, Sequence):
        return len(value)
    raise TypeError("value is not a supported sequence")


def _sequence_item(value: object, index: int) -> object:
    if isinstance(value, list):
        return list.__getitem__(value, index)
    if isinstance(value, tuple):
        return tuple.__getitem__(value, index)
    if isinstance(value, UserList):
        return list.__getitem__(_user_list_data(value), index)
    if isinstance(value, deque):
        return deque.__getitem__(value, index)
    if isinstance(value, Sequence):
        return value[index]
    raise TypeError("value is not a supported sequence")


def _sanitize_sequence(
    value: object,
    *,
    depth: int,
    active_containers: set[int],
    budget: _TraversalBudget,
) -> list[Any] | str:
    try:
        length = _sequence_length(value)
    except Exception:
        return _UNPRINTABLE_MARKER

    sanitized: list[Any] = []
    for index in range(min(length, _MAX_ARRAY_ITEMS)):
        try:
            item = _sequence_item(value, index)
        except Exception:
            sanitized.append(_UNREADABLE_MARKER)
            break
        sanitized.append(
            _sanitize(
                item,
                depth=depth + 1,
                active_containers=active_containers,
                budget=budget,
            )
        )
    if length > _MAX_ARRAY_ITEMS:
        sanitized.append({"_trace_truncated_items": length - _MAX_ARRAY_ITEMS})
    return sanitized


def _binary_summary(value: bytes | bytearray | memoryview) -> str:
    try:
        if isinstance(value, bytes):
            byte_count = bytes.__len__(value)
            kind = "bytes"
        elif isinstance(value, bytearray):
            byte_count = bytearray.__len__(value)
            kind = "bytearray"
        else:
            byte_count = value.nbytes
            kind = "memoryview"
    except Exception:
        return _UNREADABLE_MARKER
    return f"[BINARY:{kind}:{byte_count}_BYTES]"


def _safe_integer(value: int) -> int | str:
    try:
        integer = int.__int__(value)
        bit_length = int.bit_length(integer)
    except Exception:
        return _UNPRINTABLE_MARKER
    if bit_length > _MAX_INT_BITS:
        return f"[INTEGER_TOO_LARGE:{bit_length}_BITS]"
    return integer


def _safe_float(value: float) -> float | None | str:
    try:
        number = float.__float__(value)
        return number if math.isfinite(number) else None
    except Exception:
        return _UNPRINTABLE_MARKER


def _sanitize(
    value: object,
    *,
    depth: int,
    active_containers: set[int],
    budget: _TraversalBudget,
) -> dict[str, Any] | list[Any] | str | int | float | bool | None:
    if depth > _MAX_DEPTH:
        return _MAX_DEPTH_MARKER
    if not budget.consume():
        return _NODE_LIMIT_MARKER

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return _safe_integer(value)
    if isinstance(value, float):
        return _safe_float(value)
    if isinstance(value, str):
        return _bounded_string(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _binary_summary(value)

    if isinstance(value, (Mapping, list, tuple, UserList, deque, Sequence)):
        identity = id(value)
        if identity in active_containers:
            return _CIRCULAR_MARKER
        active_containers.add(identity)
        try:
            if isinstance(value, Mapping):
                return _sanitize_mapping(
                    value,
                    depth=depth,
                    active_containers=active_containers,
                    budget=budget,
                )
            return _sanitize_sequence(
                value,
                depth=depth,
                active_containers=active_containers,
                budget=budget,
            )
        finally:
            active_containers.remove(identity)

    return _safe_string(value)


def sanitize_trace_payload(
    value: object,
) -> dict[str, Any] | list[Any] | str | int | float | bool | None:
    """Return a bounded, credential-redacted, JSON-native payload."""

    return _sanitize(
        value,
        depth=0,
        active_containers=set(),
        budget=_TraversalBudget(),
    )


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

    if type(max_bytes) is not int:
        raise TypeError("max_bytes must be an integer")
    if max_bytes < 2:
        raise ValueError("max_bytes must be at least 2 bytes for a JSON object")

    sanitized = sanitize_trace_payload(value)
    document = dict(sanitized) if isinstance(sanitized, dict) else {"value": sanitized}
    original_document_bytes = len(_compact_json_bytes(document))
    if original_document_bytes <= max_bytes:
        return document

    candidates = sorted(
        ((key, len(_compact_json_bytes(item))) for key, item in document.items()),
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


def _safe_mapping_get(mapping: Mapping[str, object], key: str) -> object:
    try:
        if isinstance(mapping, dict):
            return dict.get(mapping, key, _MISSING)
        return mapping.get(key, _MISSING)
    except Exception:
        return _MISSING


def _preview_categories(value: object) -> list[str] | None:
    if not isinstance(value, (list, tuple)):
        return None
    try:
        length = _sequence_length(value)
    except Exception:
        return None

    categories: list[str] = []
    for index in range(min(length, _MAX_ARRAY_ITEMS)):
        try:
            item = _sequence_item(value, index)
        except Exception:
            return None
        if type(item) is str:
            categories.append(item)
    return categories or None


def _preview_score(value: object) -> int | float | None:
    if type(value) is int:
        return value if int.bit_length(value) <= _MAX_SCORE_INT_BITS else None
    if type(value) is float:
        return value if math.isfinite(value) else None
    return None


def _preview_item(item: object) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None

    preview: dict[str, Any] = {}
    for key in _PREVIEW_STRING_FIELDS:
        value = _safe_mapping_get(item, key)
        if type(value) is str:
            preview[key] = value

    categories = _preview_categories(_safe_mapping_get(item, "categories"))
    if categories is not None:
        preview["categories"] = categories

    score = _preview_score(_safe_mapping_get(item, "score"))
    if score is not None:
        preview["score"] = score

    if not preview:
        return None
    sanitized = sanitize_trace_payload(preview)
    return sanitized if isinstance(sanitized, dict) else None


def _trusted_total(value: object, *, result_count: int) -> int | None:
    if (
        type(value) is int
        and int.bit_length(value) <= _MAX_TOTAL_INT_BITS
        and value >= result_count
    ):
        return value
    return None


def _result_list_length(value: object) -> int:
    if not isinstance(value, list):
        return 0
    try:
        return list.__len__(value)
    except Exception:
        return 0


def trace_result_summary(
    response: Mapping[str, object],
) -> tuple[int, list[dict[str, Any]]]:
    """Return a safe result count and up to twenty allowlisted memory previews."""

    raw_results = _safe_mapping_get(response, "results")
    result_count = _result_list_length(raw_results)
    trusted_total = _trusted_total(
        _safe_mapping_get(response, "total"),
        result_count=result_count,
    )

    previews: list[dict[str, Any]] = []
    if isinstance(raw_results, list):
        scan_count = min(result_count, _MAX_PREVIEW_SCAN_ITEMS)
        for index in range(scan_count):
            try:
                item = list.__getitem__(raw_results, index)
            except Exception:
                break
            preview = _preview_item(item)
            if preview is not None:
                previews.append(preview)
            if len(previews) == _MAX_PREVIEWS:
                break

    return trusted_total if trusted_total is not None else result_count, previews
