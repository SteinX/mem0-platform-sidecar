from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.core.consolidation_service import ConsolidationService
from mem0_sidecar.store.repositories import (
    ConsolidationPolicyRepository,
    ConsolidationProposalRepository,
    ConsolidationRunRepository,
    JobRepository,
    MemoryIndexRepository,
    ProjectRepository,
    ServiceCapabilityRepository,
)
from mem0_sidecar.workers.runner import AsyncWorkerRunner


class ConsolidationScheduler:
    def __init__(
        self,
        session: Session,
        *,
        bridge_routing_required: bool = True,
    ) -> None:
        self.session = session
        self.bridge_routing_required = bridge_routing_required

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
            bridge_blocked = (
                spec.mode == "AUTO_SAFE"
                and self.bridge_routing_required
                and not ServiceCapabilityRepository(
                    self.session
                ).bridge_routing_ready(policy_row.project_id, now=now)
            )
            if bridge_blocked:
                previous = runs.last(policy_row.project_id, policy_row.app_id)
                if (
                    previous is not None
                    and previous.error_code == "BRIDGE_ROUTING_REQUIRED"
                    and previous.completed_at is not None
                    and previous.completed_at
                    > now - timedelta(seconds=spec.scan_interval_seconds)
                ):
                    continue
                run = runs.create(policy_row, now=now)
                runs.mark_failed(
                    run.id,
                    error_code="BRIDGE_ROUTING_REQUIRED",
                    error={"error_type": "BridgeRoutingRequired"},
                    completed_at=now,
                )
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


class ConsolidationWorker:
    def __init__(
        self,
        *,
        settings: SidecarSettings,
        session_factory,
        mem0_client,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.mem0_client = mem0_client

    def _bridge_ready(self, session: Session, project_id: str) -> bool:
        if not self.settings.consolidation_bridge_routing_required:
            return True
        return ServiceCapabilityRepository(session).bridge_routing_ready(project_id)

    async def handle_scan(
        self,
        _job_id: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("consolidation scan job has no run_id")
        with self.session_factory() as session:
            run = ConsolidationRunRepository(session).get(run_id)
            bridge_ready = self._bridge_ready(session, run.project_id)
            service = ConsolidationService(
                session=session,
                mem0=self.mem0_client,
                bridge_routing_ready=bridge_ready,
                hard_delete_enabled=(
                    self.settings.consolidation_hard_delete_enabled
                ),
            )
            result = await service.run_scan(run_id)
            run = ConsolidationRunRepository(session).get(run_id)
            if run.mode != "AUTO_SAFE" or not bridge_ready:
                return result
            proposals = ConsolidationProposalRepository(session)
            memories = MemoryIndexRepository(session)
            jobs = JobRepository(session)
            for proposal in proposals.list_actionable_for_run(run_id):
                source_ids = proposals.source_ids(proposal)
                sources = memories.list_memories_by_ids(
                    project_id=proposal.project_id,
                    app_id=proposal.app_id,
                    mem0_memory_ids=source_ids,
                )
                hashes = {
                    source.mem0_memory_id: source.content_hash
                    for source in sources
                    if source.content_hash is not None
                }
                if set(hashes) != set(source_ids):
                    proposals.set_status(proposal, "STALE")
                    continue
                approved = await service.approve_proposal(
                    proposal.id,
                    expected_status="PENDING",
                    expected_source_hashes=hashes,
                )
                if approved["status"] == "APPROVED":
                    jobs.enqueue(
                        project_id=proposal.project_id,
                        event_id=None,
                        job_type="consolidation.shadow",
                        payload={
                            "app_id": proposal.app_id,
                            "proposal_id": proposal.id,
                        },
                        dedupe_key=f"shadow:{proposal.id}",
                    )
            session.commit()
            return result

    async def handle_shadow(
        self,
        _job_id: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        proposal_id = payload.get("proposal_id")
        if not isinstance(proposal_id, str) or not proposal_id:
            raise ValueError("consolidation shadow job has no proposal_id")
        with self.session_factory() as session:
            proposal = ConsolidationProposalRepository(session).get(proposal_id)
            result = await ConsolidationService(
                session=session,
                mem0=self.mem0_client,
                bridge_routing_ready=self._bridge_ready(
                    session, proposal.project_id
                ),
                hard_delete_enabled=(
                    self.settings.consolidation_hard_delete_enabled
                ),
                shadow_lease_seconds=(
                    self.settings.consolidation_job_lease_seconds
                ),
            ).shadow_approved(proposal_id)
            proposal = ConsolidationProposalRepository(session).get(proposal_id)
            run = ConsolidationRunRepository(session).get(proposal.run_id)
            if (
                result["status"] == "SHADOWED"
                and run.mode == "AUTO_SAFE"
                and self.settings.consolidation_hard_delete_enabled
                and proposal.not_before is not None
            ):
                JobRepository(session).enqueue(
                    project_id=proposal.project_id,
                    event_id=None,
                    job_type="consolidation.finalize",
                    payload={
                        "app_id": proposal.app_id,
                        "proposal_id": proposal.id,
                    },
                    run_after=proposal.not_before,
                    dedupe_key=f"finalize:{proposal.id}",
                )
                session.commit()
            return result

    async def handle_finalize(
        self,
        _job_id: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        proposal_id = payload.get("proposal_id")
        if not isinstance(proposal_id, str) or not proposal_id:
            raise ValueError("consolidation finalize job has no proposal_id")
        with self.session_factory() as session:
            proposal = ConsolidationProposalRepository(session).get(proposal_id)
            bridge_ready = self._bridge_ready(session, proposal.project_id)
            return await ConsolidationService(
                session=session,
                mem0=self.mem0_client,
                bridge_routing_ready=bridge_ready,
                hard_delete_enabled=(
                    self.settings.consolidation_hard_delete_enabled
                ),
            ).finalize_shadowed(proposal_id, now=datetime.now(UTC))


class ConsolidationRuntime:
    def __init__(
        self,
        *,
        settings: SidecarSettings,
        session_factory,
        mem0_client,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.worker = ConsolidationWorker(
            settings=settings,
            session_factory=session_factory,
            mem0_client=mem0_client,
        )
        self.runner = AsyncWorkerRunner(
            session_factory=session_factory,
            handlers={
                "consolidation.scan": self.worker.handle_scan,
                "consolidation.shadow": self.worker.handle_shadow,
                "consolidation.finalize": self.worker.handle_finalize,
            },
            lease_seconds=settings.consolidation_job_lease_seconds,
        )

    async def scheduler_loop(self, stop: object) -> None:
        while not stop.is_set():
            with self.session_factory() as session:
                ConsolidationScheduler(
                    session,
                    bridge_routing_required=(
                        self.settings.consolidation_bridge_routing_required
                    ),
                ).enqueue_due_scopes()
                session.commit()
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=(
                        self.settings.consolidation_scheduler_interval_seconds
                    ),
                )
            except TimeoutError:
                pass

    async def worker_loop(self, stop: object) -> None:
        while not stop.is_set():
            handled = await self.runner.run_once()
            if handled:
                continue
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=self.settings.worker_poll_interval_seconds,
                )
            except TimeoutError:
                pass
