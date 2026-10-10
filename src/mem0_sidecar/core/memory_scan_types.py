import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol, assert_never

from pydantic import JsonValue

JsonObject = dict[str, JsonValue]
ScanMode = Literal["page", "count"]
Keyset = tuple[datetime, str]


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class MemoryGetter(Protocol):
    async def get_memory(self, memory_id: str) -> Mapping[str, JsonValue]: ...


class MemoryScanValidationError(ValueError):
    pass


class MemoryScanConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MemoryScanFilters:
    user_id: str | None = None
    agent_id: str | None = None
    run_id: str | None = None
    type: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryScanRequest:
    project_id: str
    app_id: str | None
    project_wide: bool
    filters: MemoryScanFilters = field(default_factory=MemoryScanFilters)
    mode: ScanMode = "page"
    page_size: int | None = None
    cursor: str | None = None
    include_expired: bool = False

    def __post_init__(self) -> None:
        if self.project_wide == (self.app_id is not None):
            raise MemoryScanValidationError(
                "exactly one of app_id or project_wide=true is required"
            )
        match self.mode:
            case "page":
                size = 100 if self.page_size is None else self.page_size
                if type(size) is not int or not 1 <= size <= 100:
                    raise MemoryScanValidationError(
                        "page_size must be between 1 and 100"
                    )
                object.__setattr__(self, "page_size", size)
            case "count":
                if self.page_size is not None or self.cursor is not None:
                    raise MemoryScanValidationError(
                        "count mode does not accept page_size or cursor"
                    )
            case unreachable:
                assert_never(unreachable)


@dataclass(frozen=True, slots=True)
class MemoryScanResult:
    results: tuple[JsonObject, ...]
    total: int
    next_cursor: str | None
    has_more: bool
    stale_skipped: int
    protocol: Literal["cursor-v1"] = "cursor-v1"
    count_basis: Literal["sidecar_projection"] = "sidecar_projection"


def metadata_type(metadata_json: str) -> str | None:
    try:
        value = json.loads(metadata_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    type_value = value.get("type")
    return type_value if isinstance(type_value, str) else None
