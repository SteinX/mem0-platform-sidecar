from datetime import UTC, datetime, timedelta

from mem0_sidecar.store.repositories import MemoryIndexRepository, ProjectRepository


def test_scan_candidates_use_created_at_and_id_keyset(db_session) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a", name="repo-a", mem0_base_url="http://mem0:8000"
    )
    repository = MemoryIndexRepository(db_session)
    created_at = datetime(2026, 10, 10, tzinfo=UTC)
    for memory_id in ("mem-c", "mem-a", "mem-b"):
        memory = repository.upsert_memory(
            project_id="repo-a",
            mem0_memory_id=memory_id,
            user_id="alice",
            app_id="app-a",
            category=None,
            metadata={"type": "decision"},
        )
        memory.created_at = created_at
        memory.updated_at = created_at
    repository.upsert_memory(
        project_id="repo-a",
        mem0_memory_id="expired",
        user_id="alice",
        app_id="app-a",
        category=None,
        metadata={"type": "decision"},
        expires_at=created_at - timedelta(seconds=1),
        observed_at=created_at,
    )
    db_session.commit()

    upper = repository.find_scan_upper(
        project_id="repo-a",
        app_id="app-a",
        project_wide=False,
        user_id="alice",
        agent_id=None,
        run_id=None,
        snapshot_at=created_at,
        include_expired=False,
    )
    assert upper == (created_at, "mem-c")

    first = repository.list_scan_candidates(
        project_id="repo-a",
        app_id="app-a",
        project_wide=False,
        user_id="alice",
        agent_id=None,
        run_id=None,
        snapshot_at=created_at,
        include_expired=False,
        upper=upper,
        after=None,
        limit=2,
    )
    second = repository.list_scan_candidates(
        project_id="repo-a",
        app_id="app-a",
        project_wide=False,
        user_id="alice",
        agent_id=None,
        run_id=None,
        snapshot_at=created_at,
        include_expired=False,
        upper=upper,
        after=(first[-1].created_at, first[-1].mem0_memory_id),
        limit=2,
    )

    assert [item.mem0_memory_id for item in first + second] == [
        "mem-a",
        "mem-b",
        "mem-c",
    ]

