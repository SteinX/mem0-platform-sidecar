import anyio

from mem0_sidecar.core.memory_ops import (
    MemoryUpstreamProtocolError,
    _hydrated_record_matches_projection,
    _is_upstream_not_found,
    _memory_record_from_response,
    _MemoryProjectionSnapshot,
    _normalize_memory_record,
)
from mem0_sidecar.core.memory_scan_types import JsonObject, MemoryGetter
from mem0_sidecar.mem0_client.client import Mem0UpstreamError

_HYDRATION_CONCURRENCY = 8


async def hydrate_memory_snapshots(
    mem0: MemoryGetter,
    snapshots: list[_MemoryProjectionSnapshot],
) -> dict[str, JsonObject | None]:
    hydrated: dict[str, JsonObject | None] = {}
    failures: list[Mem0UpstreamError] = []
    limiter = anyio.CapacityLimiter(_HYDRATION_CONCURRENCY)

    async def hydrate(snapshot: _MemoryProjectionSnapshot) -> None:
        try:
            async with limiter:
                response = await mem0.get_memory(snapshot.mem0_memory_id)
            record = dict(response)
            parsed = _memory_record_from_response(
                record, expected_id=snapshot.mem0_memory_id
            )
            normalized = _normalize_memory_record(parsed, projection=snapshot)
            hydrated[snapshot.mem0_memory_id] = (
                normalized
                if _hydrated_record_matches_projection(parsed, normalized, snapshot)
                else None
            )
        except (KeyError, MemoryUpstreamProtocolError, TypeError, ValueError):
            hydrated[snapshot.mem0_memory_id] = None
            return
        except RuntimeError as exc:
            if _is_upstream_not_found(exc):
                hydrated[snapshot.mem0_memory_id] = None
                return
            if isinstance(exc, Mem0UpstreamError):
                failures.append(exc)
                task_group.cancel_scope.cancel()
                return
            raise

    async with anyio.create_task_group() as task_group:
        for snapshot in snapshots:
            task_group.start_soon(hydrate, snapshot)
    if failures:
        raise failures[0]
    return hydrated
