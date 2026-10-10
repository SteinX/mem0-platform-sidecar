from datetime import UTC, datetime, timedelta

import pytest

from mem0_sidecar.core.memory_scan import (
    MemoryScanConflictError,
    MemoryScanService,
)
from mem0_sidecar.core.memory_scan_types import (
    JsonObject,
    MemoryScanFilters,
    MemoryScanRequest,
)
from mem0_sidecar.store.repositories import MemoryIndexRepository
from tests.core.memory_scan_fixtures import CURSOR_SECRET, seed_memories


@pytest.mark.asyncio
async def test_count_8568_exact_type_without_core_gets(db_session) -> None:
    core = seed_memories(db_session, 8568)
    service = MemoryScanService(
        session=db_session, mem0=core, cursor_secret=CURSOR_SECRET
    )

    result = await service.scan(
        MemoryScanRequest(
            project_id="repo-a",
            app_id="app-a",
            project_wide=False,
            filters=MemoryScanFilters(user_id="alice", type="decision"),
            mode="count",
        )
    )

    assert result.total == 3568
    assert result.results == ()
    assert core.calls == []


@pytest.mark.asyncio
async def test_keyset_traversal_8568_is_complete_and_bounded(db_session) -> None:
    core = seed_memories(db_session, 8568)
    service = MemoryScanService(
        session=db_session, mem0=core, cursor_secret=CURSOR_SECRET
    )
    cursor = None
    observed: list[str] = []

    while True:
        before = len(core.calls)
        page = await service.scan(
            MemoryScanRequest(
                project_id="repo-a",
                app_id="app-a",
                project_wide=False,
                filters=MemoryScanFilters(user_id="alice"),
                mode="page",
                page_size=100,
                cursor=cursor,
            )
        )
        observed.extend(str(item["id"]) for item in page.results)
        assert len(core.calls) - before <= 100
        assert core.max_active <= 8
        if not page.has_more:
            break
        assert page.next_cursor is not None
        cursor = page.next_cursor

    assert len(observed) == 8568
    assert len(set(observed)) == 8568
    assert len(core.calls) == 8568


@pytest.mark.asyncio
async def test_empty_type_page_advances_without_core_hydration(db_session) -> None:
    core = seed_memories(db_session, 250)
    service = MemoryScanService(
        session=db_session, mem0=core, cursor_secret=CURSOR_SECRET
    )

    page = await service.scan(
        MemoryScanRequest(
            project_id="repo-a",
            app_id="app-a",
            project_wide=False,
            filters=MemoryScanFilters(type="missing"),
            mode="page",
            page_size=100,
        )
    )

    assert page.results == ()
    assert page.total == 0
    assert page.has_more is True
    assert page.next_cursor is not None
    assert core.calls == []


@pytest.mark.asyncio
async def test_stale_projection_is_marked_and_cursor_keeps_progress(db_session) -> None:
    core = seed_memories(db_session, 2)
    del core.records["mem-00000"]
    service = MemoryScanService(
        session=db_session, mem0=core, cursor_secret=CURSOR_SECRET
    )

    page = await service.scan(
        MemoryScanRequest(
            project_id="repo-a",
            app_id="app-a",
            project_wide=False,
            page_size=1,
        )
    )

    stale = MemoryIndexRepository(db_session).get_memory(
        project_id="repo-a",
        mem0_memory_id="mem-00000",
        include_deleted=True,
    )
    assert page.results == ()
    assert page.stale_skipped == 1
    assert page.has_more is True
    assert page.next_cursor is not None
    assert stale is not None and stale.deleted_at is not None


@pytest.mark.asyncio
async def test_projection_race_requires_cursor_restart(db_session) -> None:
    core = seed_memories(db_session, 1)

    async def mutate_then_get(memory_id: str) -> JsonObject:
        memory = MemoryIndexRepository(db_session).get_memory(
            project_id="repo-a", mem0_memory_id=memory_id
        )
        assert memory is not None
        memory.updated_at = datetime(2027, 1, 1, tzinfo=UTC)
        db_session.commit()
        return core.records[memory_id]

    core.get_memory = mutate_then_get
    service = MemoryScanService(
        session=db_session, mem0=core, cursor_secret=CURSOR_SECRET
    )

    with pytest.raises(
        MemoryScanConflictError,
        match=(
            "Memory projection changed during cursor traversal; "
            "restart without a cursor"
        ),
    ):
        await service.scan(
            MemoryScanRequest(
                project_id="repo-a",
                app_id="app-a",
                project_wide=False,
                page_size=1,
            )
        )


@pytest.mark.asyncio
async def test_expiration_is_fixed_at_initial_snapshot(db_session) -> None:
    core = seed_memories(db_session, 1)
    memory = MemoryIndexRepository(db_session).get_memory(
        project_id="repo-a", mem0_memory_id="mem-00000"
    )
    assert memory is not None
    memory.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    service = MemoryScanService(
        session=db_session, mem0=core, cursor_secret=CURSOR_SECRET
    )

    active = await service.scan(
        MemoryScanRequest(
            project_id="repo-a",
            app_id="app-a",
            project_wide=False,
            mode="count",
        )
    )
    including_expired = await service.scan(
        MemoryScanRequest(
            project_id="repo-a",
            app_id="app-a",
            project_wide=False,
            mode="count",
            include_expired=True,
        )
    )

    assert active.total == 0
    assert including_expired.total == 1
