import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from mem0_sidecar.core.memory_scan import MemoryScanService
from mem0_sidecar.core.memory_scan_cursor import cursor_scope, decode_cursor
from mem0_sidecar.core.memory_scan_types import (
    MemoryScanFilters,
    MemoryScanRequest,
    as_utc,
)
from mem0_sidecar.store.repositories import MemoryIndexRepository
from tests.core.memory_scan_fixtures import CURSOR_SECRET, seed_memories


@pytest.mark.parametrize("mutation", ["reactivate", "app", "user", "type"])
@pytest.mark.asyncio
async def test_post_snapshot_scope_entrants_are_not_hydrated(
    db_session: Session, mutation: str
) -> None:
    core = seed_memories(db_session, 3)
    repository = MemoryIndexRepository(db_session)
    entrant = repository.get_memory(project_id="repo-a", mem0_memory_id="mem-00001")
    assert entrant is not None
    old_created_at = entrant.created_at
    if mutation == "reactivate":
        entrant.deleted_at = entrant.updated_at
    if mutation == "app":
        entrant.app_id = "other-app"
    if mutation == "user":
        entrant.user_id = "other-user"
    if mutation == "type":
        entrant.metadata_projection_json = json.dumps({"type": "other"})
    db_session.commit()
    request = MemoryScanRequest(
        project_id="repo-a", app_id="app-a", project_wide=False, page_size=1,
        filters=MemoryScanFilters(user_id="alice", type="Decision"),
    )
    service = MemoryScanService(
        session=db_session, mem0=core, cursor_secret=CURSOR_SECRET
    )
    first = await service.scan(request)
    assert first.total == 2
    assert [row["id"] for row in first.results] == ["mem-00000"]
    assert first.next_cursor is not None
    state = decode_cursor(first.next_cursor, cursor_scope(request), CURSOR_SECRET)
    with patch(
        "mem0_sidecar.store.repositories._utc_now",
        return_value=state.snapshot_at + timedelta(seconds=1),
    ):
        entrant = repository.upsert_memory(
            project_id="repo-a", mem0_memory_id="mem-00001", user_id="alice",
            app_id="app-a", category=None, metadata={"type": "Decision"},
        )
    db_session.commit()
    assert entrant.created_at == old_created_at
    assert as_utc(entrant.updated_at) > state.snapshot_at
    second = await service.scan(MemoryScanRequest(
        project_id="repo-a", app_id="app-a", project_wide=False, page_size=1,
        filters=request.filters, cursor=first.next_cursor,
    ))
    assert second.total == 2
    assert [row["id"] for row in second.results] == ["mem-00002"]
    assert second.has_more is False
    assert core.calls == ["mem-00000", "mem-00002"]
