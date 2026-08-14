import pytest

from mem0_sidecar.core.memory_ops import MemoryService, MemoryUpstreamProtocolError
from mem0_sidecar.store.repositories import (
    MutationIntentRepository,
    ProjectRepository,
)


class NoOpThenSuccessfulMem0Client:
    def __init__(self) -> None:
        self.add_count = 0

    async def add_memory(
        self,
        payload: dict[str, str | dict[str, str]],
    ) -> dict[str, str | list[dict[str, str]]]:
        self.add_count += 1
        if self.add_count == 1:
            return {"results": []}
        text = payload["text"]
        assert isinstance(text, str)
        return {"id": "mem-after-noop", "memory": text}


@pytest.mark.asyncio
async def test_empty_inferred_add_does_not_block_the_next_add(db_session) -> None:
    # Given a project whose first inferred add legitimately extracts no memories.
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    db_session.commit()
    mem0 = NoOpThenSuccessfulMem0Client()
    service = MemoryService(session=db_session, mem0=mem0)
    first = await service.add_memory(
        project_id="repo-a",
        payload={"text": "nothing durable", "app_id": "app-a"},
    )

    # When a later add in the same scope does produce a memory.
    second = await service.add_memory(
        project_id="repo-a",
        payload={"text": "remember this", "app_id": "app-a"},
    )

    # Then the no-op is complete and no mutation intent blocks the later write.
    assert first["memory"] == {"results": []}
    assert first["event"]["status"] == "SUCCEEDED"
    assert first["event"]["subject_id"] is None
    assert second["memory"]["id"] == "mem-after-noop"
    assert mem0.add_count == 2
    assert MutationIntentRepository(db_session).list_blocking(
        "repo-a",
        "app-a",
    ) == []


@pytest.mark.asyncio
async def test_empty_direct_add_remains_a_protocol_error(db_session) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    db_session.commit()
    mem0 = NoOpThenSuccessfulMem0Client()

    with pytest.raises(MemoryUpstreamProtocolError):
        await MemoryService(session=db_session, mem0=mem0).add_memory(
            project_id="repo-a",
            payload={
                "text": "must be stored directly",
                "app_id": "app-a",
                "infer": False,
            },
        )

    assert mem0.add_count == 1


@pytest.mark.asyncio
async def test_empty_null_infer_uses_default_inference(db_session) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    db_session.commit()
    mem0 = NoOpThenSuccessfulMem0Client()

    result = await MemoryService(session=db_session, mem0=mem0).add_memory(
        project_id="repo-a",
        payload={"text": "nothing durable", "app_id": "app-a", "infer": None},
    )

    assert result["memory"] == {"results": []}
    assert result["event"]["status"] == "SUCCEEDED"
    assert result["event"]["subject_id"] is None
