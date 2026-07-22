import asyncio

import pytest

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.store.database import create_session_factory
from mem0_sidecar.store.models import MemoryIndex
from mem0_sidecar.store.repositories import ProjectRepository
from mem0_sidecar.workers.direct_write_sync import DirectWriteSyncWorker


class _ListClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def list_memories(self, params):
        self.calls.append(dict(params))
        return {
            "results": [
                {
                    "id": "direct-memory",
                    "memory": "Direct write fact",
                    "app_id": "source-app",
                    "metadata": {"type": "auto_capture", "source": "opencode"},
                }
            ],
            "total": 1,
        }


@pytest.mark.asyncio
async def test_direct_write_sync_worker_runs_one_bounded_mirror(
    db_session,
) -> None:
    db_session_factory = create_session_factory(db_session.get_bind())
    with db_session_factory() as session:
        ProjectRepository(session).upsert_default_project(
            project_id="repo-a",
            name="repo-a",
            mem0_base_url="http://mem0.invalid",
            default_app_id="default-app",
        )
        session.commit()
    client = _ListClient()
    settings = SidecarSettings(
        database_url="sqlite://",
        mem0_base_url="http://mem0.invalid",
        default_project_id="repo-a",
        direct_write_sync_enabled=True,
        direct_write_sync_default_app_id="default-app",
        direct_write_sync_scan_limit=321,
        direct_write_sync_interval_seconds=1,
    )
    worker = DirectWriteSyncWorker(
        settings=settings,
        session_factory=db_session_factory,
        mem0_client=client,
    )

    result = await worker.run_once()

    assert result["indexed"] == 1
    assert client.calls == [{"top_k": 321, "show_expired": True}]
    with db_session_factory() as session:
        projection = session.query(MemoryIndex).filter_by(
            project_id="repo-a",
            mem0_memory_id="direct-memory",
        ).one()
        assert projection.app_id == "source-app"
        assert projection.content_hash is not None
        assert projection.content_length == len("Direct write fact")
        assert projection.normalized_type == "auto_capture"
        assert projection.source == "opencode"
        assert projection.last_observed_at is not None


@pytest.mark.asyncio
async def test_direct_write_sync_worker_stops_without_waiting_full_interval(
    db_session,
) -> None:
    db_session_factory = create_session_factory(db_session.get_bind())
    with db_session_factory() as session:
        ProjectRepository(session).upsert_default_project(
            project_id="repo-a",
            name="repo-a",
            mem0_base_url="http://mem0.invalid",
            default_app_id="default-app",
        )
        session.commit()
    settings = SidecarSettings(
        database_url="sqlite://",
        mem0_base_url="http://mem0.invalid",
        default_project_id="repo-a",
        direct_write_sync_enabled=True,
        direct_write_sync_default_app_id="default-app",
        direct_write_sync_interval_seconds=3600,
    )
    worker = DirectWriteSyncWorker(
        settings=settings,
        session_factory=db_session_factory,
        mem0_client=_ListClient(),
    )
    stop = asyncio.Event()

    task = asyncio.create_task(worker.run_forever(stop))
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=1)

    assert task.done()


@pytest.mark.asyncio
async def test_direct_write_sync_worker_logs_only_failure_type(
    db_session,
    caplog,
) -> None:
    db_session_factory = create_session_factory(db_session.get_bind())
    stop = asyncio.Event()

    class _FailingListClient:
        async def list_memories(self, params):
            stop.set()
            raise RuntimeError("raw upstream response secret")

    worker = DirectWriteSyncWorker(
        settings=SidecarSettings(
            database_url="sqlite://",
            mem0_base_url="http://mem0.invalid",
            direct_write_sync_enabled=True,
            direct_write_sync_interval_seconds=1,
        ),
        session_factory=db_session_factory,
        mem0_client=_FailingListClient(),
    )

    with caplog.at_level("ERROR", logger="mem0_sidecar.direct_write_sync"):
        await worker.run_forever(stop)

    record = caplog.records[-1]
    assert record.message == "direct_write_sync_failed"
    assert record.error_type == "RuntimeError"
    assert "raw upstream response secret" not in caplog.text
    assert record.exc_info is None
