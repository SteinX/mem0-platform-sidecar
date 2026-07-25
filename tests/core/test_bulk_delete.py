from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from mem0_sidecar.core.memory_ops import MemoryService, MutationConflictError
from mem0_sidecar.mem0_client.client import Mem0UpstreamError
from mem0_sidecar.store.models import Event, MutationIntent, MutationIntentTarget
from mem0_sidecar.store.repositories import (
    MemoryIndexRepository,
    ProjectRepository,
)


class RetryableBulkDeleteMem0Client:
    def __init__(self) -> None:
        self.records = {
            "mem-1": {"id": "mem-1", "memory": "one"},
            "mem-2": {"id": "mem-2", "memory": "two"},
        }
        self.delete_calls: list[str] = []
        self.failed_once = False

    async def get_memory(self, memory_id: str) -> dict[str, Any]:
        record = self.records.get(memory_id)
        if record is None:
            raise Mem0UpstreamError(
                method="GET",
                path=f"/memories/{memory_id}",
                status_code=404,
                response_text="not found",
                outcome_unknown=False,
                message="not found",
            )
        return record

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        self.delete_calls.append(memory_id)
        if memory_id == "mem-2" and not self.failed_once:
            self.failed_once = True
            raise Mem0UpstreamError(
                method="DELETE",
                path=f"/memories/{memory_id}",
                status_code=500,
                response_text="retry",
                outcome_unknown=False,
                message="retry",
            )
        self.records.pop(memory_id, None)
        return {"message": "Memory deleted successfully"}


def _seed_bulk_scope(session: Session) -> None:
    ProjectRepository(session).upsert_default_project(
        project_id="repo-a",
        name="repo-a",
        mem0_base_url="http://mem0.local",
        default_app_id="app-a",
    )
    memories = MemoryIndexRepository(session)
    for memory_id in ("mem-1", "mem-2"):
        memories.upsert_memory(
            project_id="repo-a",
            mem0_memory_id=memory_id,
            user_id="u1",
            app_id="app-a",
            category=None,
            metadata={},
        )
    session.commit()


@pytest.mark.asyncio
async def test_bulk_delete_retry_skips_completed_targets(
    db_session: Session,
) -> None:
    _seed_bulk_scope(db_session)
    mem0 = RetryableBulkDeleteMem0Client()
    service = MemoryService(session=db_session, mem0=mem0)

    with pytest.raises(MutationConflictError, match="retry"):
        await service.bulk_delete_memories(
            project_id="repo-a",
            app_id="app-a",
            filters={"user_id": "u1"},
        )

    result = await service.bulk_delete_memories(
        project_id="repo-a",
        app_id="app-a",
        filters={"user_id": "u1"},
    )

    assert result == {"message": "All relevant memories deleted"}
    assert mem0.delete_calls.count("mem-1") == 1
    assert mem0.delete_calls.count("mem-2") == 2
    assert MemoryIndexRepository(db_session).list_scoped_memory_ids(
        project_id="repo-a",
        mem0_memory_ids=["mem-1", "mem-2"],
        user_id="u1",
        app_id="app-a",
        agent_id=None,
        run_id=None,
    ) == set()

    intents = list(
        db_session.scalars(
            select(MutationIntent).where(
                MutationIntent.operation == "memory.bulk_delete"
            )
        )
    )
    assert len(intents) == 1
    assert intents[0].status == "COMPLETED"
    assert {
        target.status
        for target in db_session.scalars(
            select(MutationIntentTarget).where(
                MutationIntentTarget.intent_id == intents[0].id
            )
        )
    } == {"COMPLETED"}

    events = list(
        db_session.scalars(
            select(Event)
            .where(Event.operation == "memory.bulk_delete")
            .order_by(Event.created_at)
        )
    )
    assert [event.status for event in events] == ["FAILED", "SUCCEEDED"]


@pytest.mark.asyncio
async def test_completed_bulk_delete_does_not_suppress_later_matching_memories(
    db_session: Session,
) -> None:
    _seed_bulk_scope(db_session)
    mem0 = RetryableBulkDeleteMem0Client()
    mem0.failed_once = True
    service = MemoryService(session=db_session, mem0=mem0)

    first = await service.bulk_delete_memories(
        project_id="repo-a",
        app_id="app-a",
        filters={"user_id": "u1"},
    )
    MemoryIndexRepository(db_session).upsert_memory(
        project_id="repo-a",
        mem0_memory_id="mem-later",
        user_id="u1",
        app_id="app-a",
        category=None,
        metadata={},
    )
    db_session.commit()
    mem0.records["mem-later"] = {"id": "mem-later", "memory": "later"}

    second = await service.bulk_delete_memories(
        project_id="repo-a",
        app_id="app-a",
        filters={"user_id": "u1"},
    )

    assert first == second == {"message": "All relevant memories deleted"}
    assert mem0.delete_calls.count("mem-later") == 1
    assert (
        len(
            list(
                db_session.scalars(
                    select(MutationIntent).where(
                        MutationIntent.operation == "memory.bulk_delete"
                    )
                )
            )
        )
        == 2
    )
