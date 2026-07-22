from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from mem0_sidecar.store.repositories import (
    ConsolidationPolicyRepository,
    ConsolidationRunRepository,
    JobRepository,
    MemoryIndexRepository,
    ProjectRepository,
)


class ConsolidationScheduler:
    def __init__(self, session: Session) -> None:
        self.session = session

    def enqueue_due_scopes(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        policies = ConsolidationPolicyRepository(self.session)
        runs = ConsolidationRunRepository(self.session)
        jobs = JobRepository(self.session)
        memories = MemoryIndexRepository(self.session)
        enqueued = 0

        for policy_row in policies.list_enabled():
            spec = policies.spec(policy_row)
            if runs.active(policy_row.project_id, policy_row.app_id) is not None:
                continue
            dirty = memories.list_dirty_anchors(
                project_id=policy_row.project_id,
                app_id=policy_row.app_id,
                limit=spec.min_new_memories,
            )
            last_success = runs.last_succeeded(
                policy_row.project_id,
                policy_row.app_id,
            )
            interval_due = (
                last_success is not None
                and last_success.completed_at is not None
                and last_success.completed_at
                <= now - timedelta(seconds=spec.scan_interval_seconds)
            )
            if len(dirty) < spec.min_new_memories and not interval_due:
                continue

            ProjectRepository(self.session).lock_for_mutation(
                policy_row.project_id
            )
            if runs.active(policy_row.project_id, policy_row.app_id) is not None:
                continue
            run = runs.create(policy_row, now=now)
            window = int(now.timestamp()) // spec.scan_interval_seconds
            jobs.enqueue(
                project_id=policy_row.project_id,
                event_id=None,
                job_type="consolidation.scan",
                payload={"app_id": policy_row.app_id, "run_id": run.id},
                dedupe_key=f"{policy_row.app_id}:{window}",
            )
            enqueued += 1

        self.session.flush()
        return enqueued
