from datetime import UTC, datetime, timedelta

import pytest

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.core.memory_ops import memory_content_fingerprint
from mem0_sidecar.store.database import create_engine_from_url, create_session_factory
from mem0_sidecar.store.models import (
    Base,
    ConsolidationProposal,
    ConsolidationRun,
    Job,
    MemoryIndex,
)
from mem0_sidecar.store.repositories import (
    ConsolidationPolicyRepository,
    JobRepository,
    MemoryIndexRepository,
    ProjectRepository,
    ServiceCapabilityRepository,
)
from mem0_sidecar.workers.consolidation import (
    ConsolidationRuntime,
    ConsolidationScheduler,
)


class RuntimeMem0:
    def __init__(self) -> None:
        self.records = {
            "mem-a": {"id": "mem-a", "memory": "same"},
            "mem-b": {"id": "mem-b", "memory": "same"},
        }

    async def get_memory(self, memory_id: str):
        return self.records[memory_id]

    async def search_memories(self, _payload):
        return {"results": []}


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


def test_auto_safe_requires_current_bridge_routing_heartbeat(db_session) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    ConsolidationPolicyRepository(db_session).upsert(
        project_id="repo-a",
        app_id="app-a",
        policy={
            "enabled": True,
            "mode": "AUTO_SAFE",
            "min_new_memories": 1,
        },
    )
    observed = datetime(2026, 7, 23, 12, tzinfo=UTC)
    MemoryIndexRepository(db_session).upsert_memory(
        project_id="repo-a",
        mem0_memory_id="mem-a",
        user_id="root",
        app_id="app-a",
        category="decision",
        content_hash="hash-a",
        content_length=10,
        normalized_type="decision",
        source="manual",
        pinned=False,
        observed_at=observed,
    )
    db_session.commit()

    assert ConsolidationScheduler(db_session).enqueue_due_scopes(now=observed) == 0
    failed = db_session.query(ConsolidationRun).one()
    assert failed.status == "FAILED"
    assert failed.error_code == "BRIDGE_ROUTING_REQUIRED"
    assert db_session.query(Job).count() == 0

    capabilities = ServiceCapabilityRepository(db_session)
    capabilities.record_bridge_heartbeat(
        project_id="repo-a",
        instance_id="bridge-a",
        bridge_version="0.1.1",
        routes_reads=True,
        routes_writes=True,
        observed_at=observed,
    )
    db_session.commit()

    assert ConsolidationScheduler(db_session).enqueue_due_scopes(now=observed) == 1
    assert db_session.query(Job).count() == 1
    assert capabilities.bridge_routing_ready(
        "repo-a", now=observed + timedelta(minutes=9)
    )
    assert not capabilities.bridge_routing_ready(
        "repo-a", now=observed + timedelta(minutes=11)
    )


@pytest.mark.asyncio
async def test_runtime_worker_scans_auto_approves_and_shadows(tmp_path) -> None:
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'runtime.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    now = datetime.now(UTC)
    content_hash, content_length = memory_content_fingerprint({"memory": "same"})
    assert content_hash is not None
    with factory() as session:
        ProjectRepository(session).upsert_default_project(
            project_id="repo-a",
            name="Repo A",
            mem0_base_url="http://mem0:8000",
        )
        ConsolidationPolicyRepository(session).upsert(
            project_id="repo-a",
            app_id="app-a",
            policy={
                "enabled": True,
                "mode": "AUTO_SAFE",
                "min_new_memories": 2,
            },
        )
        for memory_id in ("mem-a", "mem-b"):
            MemoryIndexRepository(session).upsert_memory(
                project_id="repo-a",
                mem0_memory_id=memory_id,
                user_id="root",
                app_id="app-a",
                category="decision",
                content_hash=content_hash,
                content_length=content_length,
                normalized_type="decision",
                source="manual",
                pinned=False,
                observed_at=now,
            )
        assert ConsolidationScheduler(
            session, bridge_routing_required=False
        ).enqueue_due_scopes(now=now) == 1
        session.commit()

    runtime = ConsolidationRuntime(
        settings=SidecarSettings(
            consolidation_enabled=True,
            consolidation_bridge_routing_required=False,
        ),
        session_factory=factory,
        mem0_client=RuntimeMem0(),
    )
    assert await runtime.runner.run_once()
    assert await runtime.runner.run_once()

    with factory() as session:
        proposal = session.query(ConsolidationProposal).one()
        projections = {
            memory.mem0_memory_id: memory.consolidation_state
            for memory in session.query(MemoryIndex).all()
        }
        jobs = session.query(Job).order_by(Job.created_at).all()
    assert proposal.status == "SHADOWED"
    assert sorted(projections.values()) == ["ACTIVE", "SHADOWED"]
    assert [job.status.value for job in jobs] == ["SUCCEEDED", "SUCCEEDED"]
