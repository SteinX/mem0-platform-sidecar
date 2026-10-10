from datetime import UTC, datetime

import anyio

from mem0_sidecar.core.memory_scan_types import JsonObject
from mem0_sidecar.store.repositories import MemoryIndexRepository, ProjectRepository

CURSOR_SECRET = b"cursor-secret-for-memory-scan-tests"


class CountingCore:
    def __init__(self, records: dict[str, JsonObject]) -> None:
        self.records = records
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0

    async def get_memory(self, memory_id: str) -> JsonObject:
        self.calls.append(memory_id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await anyio.sleep(0)
        try:
            return self.records[memory_id]
        finally:
            self.active -= 1


def seed_memories(db_session, count: int) -> CountingCore:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a", name="repo-a", mem0_base_url="http://mem0:8000"
    )
    repository = MemoryIndexRepository(db_session)
    records: dict[str, JsonObject] = {}
    created_at = datetime(2026, 10, 10, tzinfo=UTC)
    for index in range(count):
        memory_id = f"mem-{index:05d}"
        type_name = "decision" if index >= 5000 else "Decision"
        memory = repository.upsert_memory(
            project_id="repo-a",
            mem0_memory_id=memory_id,
            user_id="alice",
            app_id="app-a",
            category=None,
            metadata={"type": type_name},
        )
        memory.created_at = created_at
        memory.updated_at = created_at
        records[memory_id] = {
            "id": memory_id,
            "memory": memory_id,
            "user_id": "alice",
            "app_id": "app-a",
            "metadata": {"type": type_name},
        }
    db_session.commit()
    return CountingCore(records)
