from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from mem0_sidecar.core.memory_scan import MemoryScanService
from mem0_sidecar.core.memory_scan_types import (
    MemoryScanConflictError,
    MemoryScanRequest,
)
from mem0_sidecar.store.models import Entity
from mem0_sidecar.store.repositories import EntityRepository, MemoryIndexRepository
from tests.core.memory_scan_fixtures import CURSOR_SECRET, seed_memories


@pytest.mark.asyncio
async def test_stale_scan_refreshes_counts_and_removes_empty_entities(
    db_session: Session,
) -> None:
    core = seed_memories(db_session, 2)
    EntityRepository(db_session).rebuild_project_entities("repo-a", "app-a")
    db_session.commit()
    core.records.clear()
    service = MemoryScanService(
        session=db_session, mem0=core, cursor_secret=CURSOR_SECRET
    )
    first = await service.scan(
        MemoryScanRequest(
            project_id="repo-a", app_id="app-a", project_wide=False, page_size=1
        )
    )
    db_session.commit()
    entities = list(db_session.scalars(select(Entity)))
    assert {(row.entity_type, row.entity_id, row.memory_count) for row in entities} == {
        ("app", "app-a", 1),
        ("user", "alice", 1),
    }
    assert first.stale_skipped == 1
    assert first.next_cursor is not None
    second = await service.scan(
        MemoryScanRequest(
            project_id="repo-a",
            app_id="app-a",
            project_wide=False,
            page_size=1,
            cursor=first.next_cursor,
        )
    )
    db_session.commit()
    assert second.stale_skipped == 1
    assert list(db_session.scalars(select(Entity))) == []


@pytest.mark.asyncio
async def test_failed_conditional_stale_mark_rejects_page(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = seed_memories(db_session, 1)
    core.records.clear()
    original_mark = MemoryIndexRepository.mark_stale_if_unchanged
    changed_at = datetime(2027, 1, 1, tzinfo=UTC)

    def race_then_mark(
        repository: MemoryIndexRepository,
        *,
        project_id: str,
        app_id: str | None,
        mem0_memory_ids: Iterable[str],
        updated_at_lte: datetime,
        expected_updated_at: Mapping[str, datetime] | None = None,
    ) -> int:
        memory = repository.get_memory(project_id="repo-a", mem0_memory_id="mem-00000")
        assert memory is not None
        memory.updated_at = changed_at
        db_session.commit()
        return original_mark(
            repository,
            project_id=project_id,
            app_id=app_id,
            mem0_memory_ids=mem0_memory_ids,
            updated_at_lte=updated_at_lte,
            expected_updated_at=expected_updated_at,
        )

    monkeypatch.setattr(
        MemoryIndexRepository, "mark_stale_if_unchanged", race_then_mark
    )
    service = MemoryScanService(
        session=db_session, mem0=core, cursor_secret=CURSOR_SECRET
    )
    with pytest.raises(MemoryScanConflictError, match="restart without a cursor"):
        await service.scan(
            MemoryScanRequest(
                project_id="repo-a", app_id="app-a", project_wide=False, page_size=1
            )
        )
    memory = MemoryIndexRepository(db_session).get_memory(
        project_id="repo-a", mem0_memory_id="mem-00000"
    )
    assert memory is not None
    assert memory.updated_at.replace(tzinfo=UTC) == changed_at
