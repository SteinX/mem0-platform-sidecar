import threading
from pathlib import Path

import anyio
import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from mem0_sidecar.core.memory_scan import MemoryScanService
from mem0_sidecar.core.memory_scan_types import MemoryScanFilters, MemoryScanRequest
from mem0_sidecar.store.database import create_engine_from_url, create_session_factory
from mem0_sidecar.store.models import Base, Entity, Project
from mem0_sidecar.store.repositories import (
    EntityRepository,
    MemoryIndexRepository,
    ProjectRepository,
)
from tests.core.memory_scan_fixtures import CURSOR_SECRET, CountingCore, seed_memories


def test_competing_stale_scans_serialize_entity_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'scan-lock.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        seed_memories(session, 2)
        repository = MemoryIndexRepository(session)
        for index, run_id in enumerate(("run-a", "run-b")):
            memory = repository.get_memory(
                project_id="repo-a", mem0_memory_id=f"mem-{index:05d}"
            )
            assert memory is not None
            memory.run_id = run_id
        EntityRepository(session).rebuild_project_entities("repo-a", "app-a")
        session.commit()

    locked_a, release_a = threading.Event(), threading.Event()
    attempted_b, acquired_b = threading.Event(), threading.Event()
    original_lock = ProjectRepository.lock_for_mutation

    def pause_after_lock(repository: ProjectRepository, project_id: str) -> Project:
        is_a = threading.current_thread().name == "scan-a"
        if not is_a:
            attempted_b.set()
        project = original_lock(repository, project_id)
        if is_a:
            locked_a.set()
            assert release_a.wait(5)
        else:
            acquired_b.set()
        return project

    monkeypatch.setattr(ProjectRepository, "lock_for_mutation", pause_after_lock)
    failures: list[BaseException] = []

    def scan(run_id: str) -> None:
        try:
            with factory() as session:
                service = MemoryScanService(
                    session=session, mem0=CountingCore({}), cursor_secret=CURSOR_SECRET
                )
                result = anyio.run(
                    service.scan,
                    MemoryScanRequest(
                        project_id="repo-a",
                        app_id="app-a",
                        project_wide=False,
                        filters=MemoryScanFilters(run_id=run_id),
                        page_size=1,
                    ),
                )
                assert result.stale_skipped == 1
                session.commit()
        except (AssertionError, RuntimeError, SQLAlchemyError) as exc:
            failures.append(exc)

    first = threading.Thread(target=scan, args=("run-a",), name="scan-a")
    second = threading.Thread(target=scan, args=("run-b",), name="scan-b")
    first.start()
    try:
        assert locked_a.wait(2)
        second.start()
        assert attempted_b.wait(2)
        assert not acquired_b.wait(0.1)
    finally:
        release_a.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert failures == []
    assert acquired_b.is_set()
    with factory() as session:
        assert list(session.scalars(select(Entity))) == []
    engine.dispose()
