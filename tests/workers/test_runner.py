from mem0_sidecar.store.models import JobStatus
from mem0_sidecar.store.repositories import JobRepository, ProjectRepository
from mem0_sidecar.workers.runner import WorkerRunner


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
