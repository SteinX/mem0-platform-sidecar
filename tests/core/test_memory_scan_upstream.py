import anyio
import pytest
from sqlalchemy.orm import Session

from mem0_sidecar.core.memory_scan import MemoryScanService
from mem0_sidecar.core.memory_scan_types import JsonObject, MemoryScanRequest
from mem0_sidecar.mem0_client.client import Mem0UpstreamError
from tests.core.memory_scan_fixtures import CURSOR_SECRET, seed_memories


class FailingConcurrentCore:
    def __init__(self) -> None:
        self.sibling_started = anyio.Event()
        self.sibling_cancelled = False
        self.failure = Mem0UpstreamError(
            method="GET", path="/memories/mem-00000", message="fixture failure"
        )

    async def get_memory(self, memory_id: str) -> JsonObject:
        if memory_id == "mem-00000":
            await self.sibling_started.wait()
            raise self.failure
        self.sibling_started.set()
        try:
            await anyio.sleep_forever()
        finally:
            self.sibling_cancelled = True
        raise AssertionError("a cancelled sibling must not return")


@pytest.mark.asyncio
async def test_scan_cancels_sibling_and_raises_original_upstream_error(
    db_session: Session,
) -> None:
    seed_memories(db_session, 2)
    core = FailingConcurrentCore()
    service = MemoryScanService(
        session=db_session, mem0=core, cursor_secret=CURSOR_SECRET
    )
    with anyio.fail_after(2), pytest.raises(Mem0UpstreamError) as caught:
        await service.scan(
            MemoryScanRequest(project_id="repo-a", app_id="app-a", project_wide=False)
        )
    assert caught.value is core.failure
    assert core.sibling_cancelled
