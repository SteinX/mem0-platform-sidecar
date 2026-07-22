import asyncio
import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from mem0_sidecar.store.repositories import JobRepository

JobHandler = Callable[
    [str, dict[str, object]],
    dict[str, object] | Awaitable[dict[str, object]],
]


class WorkerRunner:
    """Compatibility facade for legacy callers that only claim a job."""

    def __init__(self, jobs: JobRepository) -> None:
        self.jobs = jobs

    def run_once(self) -> bool:
        job = self.jobs.claim_next()
        return job is not None


class AsyncWorkerRunner:
    def __init__(
        self,
        *,
        session_factory: Any,
        handlers: dict[str, JobHandler],
        lease_seconds: int = 300,
    ) -> None:
        self.session_factory = session_factory
        self.handlers = dict(handlers)
        self.lease_seconds = lease_seconds

    async def run_once(self) -> bool:
        with self.session_factory() as session:
            jobs = JobRepository(session)
            job = jobs.claim_next(
                job_types=tuple(self.handlers),
                lease_seconds=self.lease_seconds,
            )
            if job is None:
                session.rollback()
                return False
            job_id = job.id
            job_type = job.job_type
            attempt_count = job.attempt_count
            payload = jobs.payload(job)
            session.commit()

        handler = self.handlers[job_type]
        try:
            result = handler(job_id, payload)
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, dict):
                raise TypeError("job handler result must be a mapping")
        except asyncio.CancelledError:
            await self._record_failure(job_id, attempt_count, "CancelledError")
            raise
        except Exception as exc:
            await self._record_failure(job_id, attempt_count, type(exc).__name__)
            return True

        with self.session_factory() as session:
            JobRepository(session).mark_succeeded(job_id, result)
            session.commit()
        return True

    async def _record_failure(
        self,
        job_id: str,
        attempt_count: int,
        error_type: str,
    ) -> None:
        delay_seconds = min(3600, (2**attempt_count) * 30)
        retry_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        with self.session_factory() as session:
            JobRepository(session).mark_failed(
                job_id,
                {"error_type": error_type},
                retry_at,
            )
            session.commit()
