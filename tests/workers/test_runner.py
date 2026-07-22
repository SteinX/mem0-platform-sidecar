from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.dialects import postgresql

from mem0_sidecar.store.database import create_session_factory
from mem0_sidecar.store.models import Job, JobStatus
from mem0_sidecar.store.repositories import JobRepository, ProjectRepository
from mem0_sidecar.workers.runner import AsyncWorkerRunner, WorkerRunner


def test_worker_runner_claims_one_pending_job(db_session) -> None:
    project = ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    job_repo = JobRepository(db_session)
    job = job_repo.enqueue(
        project_id=project.id,
        event_id=None,
        job_type="entity.rebuild",
        payload={},
    )
    db_session.commit()

    did_work = WorkerRunner(job_repo).run_once()
    db_session.commit()

    assert did_work is True
    assert db_session.get(type(job), job.id).status is JobStatus.RUNNING


def test_job_claim_honors_type_time_lease_and_attempt_exhaustion(db_session) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    jobs = JobRepository(db_session)
    now = datetime(2026, 7, 23, 12, tzinfo=UTC)
    future = jobs.enqueue(
        project_id="repo-a",
        event_id=None,
        job_type="consolidation.scan",
        payload={},
        run_after=now + timedelta(minutes=1),
    )
    other = jobs.enqueue(
        project_id="repo-a",
        event_id=None,
        job_type="entity.rebuild",
        payload={},
    )
    eligible = jobs.enqueue(
        project_id="repo-a",
        event_id=None,
        job_type="consolidation.scan",
        payload={},
        max_attempts=2,
    )
    db_session.commit()

    claimed = jobs.claim_next(
        job_types=("consolidation.scan",), lease_seconds=30, now=now
    )
    assert claimed is not None and claimed.id == eligible.id
    assert claimed.attempt_count == 1
    assert claimed.lease_expires_at is not None
    assert claimed.lease_expires_at.replace(tzinfo=UTC) == now + timedelta(
        seconds=30
    )
    assert future.status is JobStatus.PENDING
    assert other.status is JobStatus.PENDING

    claimed.lease_expires_at = now - timedelta(seconds=1)
    db_session.commit()
    recovered = jobs.claim_next(
        job_types=("consolidation.scan",), lease_seconds=30, now=now
    )
    assert recovered is not None and recovered.id == eligible.id
    assert recovered.attempt_count == 2

    recovered.lease_expires_at = now - timedelta(seconds=1)
    db_session.commit()
    assert (
        jobs.claim_next(
            job_types=("consolidation.scan",), lease_seconds=30, now=now
        )
        is None
    )
    db_session.expire_all()
    assert db_session.get(Job, eligible.id).status is JobStatus.FAILED


def test_job_claim_statement_uses_postgres_skip_locked(db_session) -> None:
    statement = JobRepository(db_session)._claim_candidate_statement(
        job_types=("consolidation.scan",),
        now=datetime(2026, 7, 23, 12, tzinfo=UTC),
        skip_locked=True,
    )

    sql = str(statement.compile(dialect=postgresql.dialect())).upper()
    assert "FOR UPDATE SKIP LOCKED" in sql


@pytest.mark.asyncio
async def test_async_worker_dispatches_and_persists_success(db_session) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    job = JobRepository(db_session).enqueue(
        project_id="repo-a",
        event_id=None,
        job_type="consolidation.scan",
        payload={"run_id": "run-a"},
    )
    db_session.commit()
    calls: list[tuple[str, dict[str, object]]] = []

    async def handler(job_id: str, payload: dict[str, object]):
        calls.append((job_id, payload))
        return {"proposal_count": 2}

    runner = AsyncWorkerRunner(
        session_factory=create_session_factory(db_session.get_bind()),
        handlers={"consolidation.scan": handler},
        lease_seconds=30,
    )

    assert await runner.run_once() is True
    db_session.expire_all()
    persisted = db_session.get(Job, job.id)
    assert calls == [(job.id, {"run_id": "run-a"})]
    assert persisted.status is JobStatus.SUCCEEDED
    assert persisted.result_json == '{"proposal_count": 2}'
    assert persisted.lease_expires_at is None


@pytest.mark.asyncio
async def test_async_worker_retries_without_persisting_exception_text(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    job = JobRepository(db_session).enqueue(
        project_id="repo-a",
        event_id=None,
        job_type="consolidation.scan",
        payload={},
    )
    db_session.commit()

    async def handler(_job_id: str, _payload: dict[str, object]):
        raise RuntimeError("secret upstream body")

    runner = AsyncWorkerRunner(
        session_factory=create_session_factory(db_session.get_bind()),
        handlers={"consolidation.scan": handler},
        lease_seconds=30,
    )

    assert await runner.run_once() is True
    db_session.expire_all()
    persisted = db_session.get(Job, job.id)
    assert persisted.status is JobStatus.PENDING
    assert persisted.run_after is not None
    assert "RuntimeError" in persisted.error_json
    assert "secret upstream body" not in persisted.error_json
