from datetime import UTC, datetime

from mem0_sidecar.store.models import ConsolidationRun, Job
from mem0_sidecar.store.repositories import (
    ConsolidationPolicyRepository,
    JobRepository,
    MemoryIndexRepository,
    ProjectRepository,
)
from mem0_sidecar.workers.consolidation import ConsolidationScheduler


def test_scheduler_enqueues_one_due_scope_and_refuses_duplicate(db_session) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    policy = ConsolidationPolicyRepository(db_session).upsert(
        project_id="repo-a",
        app_id="app-a",
        policy={
            "enabled": True,
            "mode": "OBSERVE",
            "min_new_memories": 2,
            "scan_interval_seconds": 3600,
        },
    )
    memories = MemoryIndexRepository(db_session)
    observed = datetime(2026, 7, 23, 12, tzinfo=UTC)
    for memory_id in ("mem-a", "mem-b"):
        memories.upsert_memory(
            project_id="repo-a",
            mem0_memory_id=memory_id,
            user_id="root",
            app_id="app-a",
            category="decision",
            content_hash=f"hash-{memory_id}",
            content_length=10,
            normalized_type="decision",
            source="manual",
            pinned=False,
            observed_at=observed,
        )
    db_session.commit()
    scheduler = ConsolidationScheduler(db_session)

    assert scheduler.enqueue_due_scopes(now=observed) == 1
    db_session.commit()
    assert scheduler.enqueue_due_scopes(now=observed) == 0

    run = db_session.query(ConsolidationRun).one()
    job = db_session.query(Job).one()
    assert run.policy_id == policy.id
    assert run.mode == "OBSERVE"
    assert JobRepository(db_session).payload(job) == {
        "app_id": "app-a",
        "run_id": run.id,
    }
    assert job.job_type == "consolidation.scan"
