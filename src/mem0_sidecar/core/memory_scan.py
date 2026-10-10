from datetime import UTC, datetime

from sqlalchemy.orm import Session

from mem0_sidecar.core.memory_ops import (
    _projection_matches_snapshot,
    _snapshot_memory_projection,
)
from mem0_sidecar.core.memory_scan_cursor import (
    CursorState,
    cursor_scope,
    decode_cursor,
    encode_cursor,
)
from mem0_sidecar.core.memory_scan_hydration import hydrate_memory_snapshots
from mem0_sidecar.core.memory_scan_types import (
    Keyset,
    MemoryGetter,
    MemoryScanConflictError,
    MemoryScanRequest,
    MemoryScanResult,
    as_utc,
    metadata_type,
)
from mem0_sidecar.store.models import MemoryIndex
from mem0_sidecar.store.repositories import MemoryIndexRepository

_COUNT_BATCH_SIZE = 200


class MemoryScanService:
    def __init__(
        self, *, session: Session, mem0: MemoryGetter, cursor_secret: bytes
    ) -> None:
        self.session = session
        self.mem0 = mem0
        self.cursor_secret = cursor_secret

    def _candidates(
        self,
        request: MemoryScanRequest,
        *,
        snapshot_at: datetime,
        upper: Keyset | None,
        after: Keyset | None,
        limit: int,
    ) -> list[MemoryIndex]:
        return MemoryIndexRepository(self.session).list_scan_candidates(
            project_id=request.project_id,
            app_id=request.app_id,
            project_wide=request.project_wide,
            user_id=request.filters.user_id,
            agent_id=request.filters.agent_id,
            run_id=request.filters.run_id,
            snapshot_at=snapshot_at,
            include_expired=request.include_expired,
            upper=upper,
            after=after,
            limit=limit,
        )

    def _initial_total(
        self,
        request: MemoryScanRequest,
        *,
        snapshot_at: datetime,
        upper: Keyset | None,
    ) -> int:
        repository = MemoryIndexRepository(self.session)
        if request.filters.type is None:
            return repository.count_scan_rows(
                project_id=request.project_id,
                app_id=request.app_id,
                project_wide=request.project_wide,
                user_id=request.filters.user_id,
                agent_id=request.filters.agent_id,
                run_id=request.filters.run_id,
                snapshot_at=snapshot_at,
                include_expired=request.include_expired,
                upper=upper,
            )
        total = 0
        after: Keyset | None = None
        while candidates := self._candidates(
            request,
            snapshot_at=snapshot_at,
            upper=upper,
            after=after,
            limit=_COUNT_BATCH_SIZE,
        ):
            total += sum(
                metadata_type(item.metadata_projection_json) == request.filters.type
                for item in candidates
            )
            last = candidates[-1]
            after = (as_utc(last.created_at), last.mem0_memory_id)
        return total

    async def scan(self, request: MemoryScanRequest) -> MemoryScanResult:
        scope = cursor_scope(request)
        cursor = (
            decode_cursor(request.cursor, scope, self.cursor_secret)
            if request.cursor
            else None
        )
        if cursor is None:
            snapshot_at = datetime.now(UTC)
            repository = MemoryIndexRepository(self.session)
            upper = repository.find_scan_upper(
                project_id=request.project_id,
                app_id=request.app_id,
                project_wide=request.project_wide,
                user_id=request.filters.user_id,
                agent_id=request.filters.agent_id,
                run_id=request.filters.run_id,
                snapshot_at=snapshot_at,
                include_expired=request.include_expired,
            )
            total = self._initial_total(
                request, snapshot_at=snapshot_at, upper=upper
            )
            after = None
        else:
            snapshot_at = cursor.snapshot_at
            upper = cursor.upper
            after = cursor.after
            total = cursor.total

        if request.mode == "count" or upper is None:
            self.session.rollback()
            return MemoryScanResult((), total, None, False, 0)

        page_size = request.page_size
        if page_size is None:
            raise AssertionError("page mode requires a page size")
        raw_limit = min(page_size * 2, 200)
        candidates = self._candidates(
            request,
            snapshot_at=snapshot_at,
            upper=upper,
            after=after,
            limit=raw_limit,
        )
        examined = []
        snapshots = []
        for item in candidates:
            examined.append(item)
            if request.filters.type is None or (
                metadata_type(item.metadata_projection_json) == request.filters.type
            ):
                snapshots.append(_snapshot_memory_projection(item))
            if len(snapshots) == page_size:
                break
        raw_after = (
            (as_utc(examined[-1].created_at), examined[-1].mem0_memory_id)
            if examined
            else after
        )
        has_more = bool(
            raw_after is not None
            and self._candidates(
                request,
                snapshot_at=snapshot_at,
                upper=upper,
                after=raw_after,
                limit=1,
            )
        )
        self.session.rollback()

        hydrated = await hydrate_memory_snapshots(self.mem0, snapshots)

        repository = MemoryIndexRepository(self.session)
        current = {
            item.mem0_memory_id: item
            for item in repository.list_memories_by_ids(
                project_id=request.project_id,
                app_id=request.app_id,
                mem0_memory_ids=[item.mem0_memory_id for item in snapshots],
            )
        }
        if any(
            (item := current.get(snapshot.mem0_memory_id)) is None
            or not _projection_matches_snapshot(item, snapshot)
            for snapshot in snapshots
        ):
            self.session.rollback()
            raise MemoryScanConflictError(
                "Memory projection changed during cursor traversal; "
                "restart without a cursor"
            )
        stale = [
            snapshot
            for snapshot in snapshots
            if hydrated[snapshot.mem0_memory_id] is None
        ]
        if stale:
            repository.mark_stale_if_unchanged(
                project_id=request.project_id,
                app_id=request.app_id,
                mem0_memory_ids=[item.mem0_memory_id for item in stale],
                updated_at_lte=max(item.updated_at for item in stale),
                expected_updated_at={
                    item.mem0_memory_id: item.updated_at for item in stale
                },
            )
        results = tuple(
            record
            for snapshot in snapshots
            if (record := hydrated[snapshot.mem0_memory_id]) is not None
        )
        next_cursor = (
            encode_cursor(
                CursorState(
                    snapshot_at=snapshot_at,
                    upper=upper,
                    after=raw_after,
                    total=total,
                ),
                scope,
                self.cursor_secret,
            )
            if has_more and raw_after is not None
            else None
        )
        return MemoryScanResult(
            results=results,
            total=total,
            next_cursor=next_cursor,
            has_more=has_more,
            stale_skipped=len(stale),
        )
