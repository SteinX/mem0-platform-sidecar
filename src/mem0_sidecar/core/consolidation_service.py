from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from mem0_sidecar.core.consolidation_analysis import ConsolidationAnalyzer
from mem0_sidecar.core.consolidation_policy import ConsolidationPolicySpec
from mem0_sidecar.store.models import (
    ConsolidationProposal,
    ConsolidationRun,
)
from mem0_sidecar.store.repositories import (
    ConsolidationPolicyRepository,
    ConsolidationProposalRepository,
    ConsolidationRunRepository,
    MemoryIndexRepository,
)


def _json_object(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _run_payload(run: ConsolidationRun) -> dict[str, object]:
    return {
        "id": run.id,
        "project_id": run.project_id,
        "app_id": run.app_id,
        "mode": run.mode,
        "status": run.status,
        "scan_cutoff": run.scan_cutoff,
        "counts": _json_object(run.counts_json),
        "error_code": run.error_code,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
    }


def _proposal_payload(proposal: ConsolidationProposal) -> dict[str, object]:
    try:
        source_ids = json.loads(proposal.source_ids_json)
    except (TypeError, ValueError):
        source_ids = []
    return {
        "id": proposal.id,
        "run_id": proposal.run_id,
        "kind": proposal.kind,
        "status": proposal.status,
        "source_ids": source_ids if isinstance(source_ids, list) else [],
        "canonical_id": proposal.canonical_memory_id,
        "score": proposal.score,
        "evidence": _json_object(proposal.evidence_json),
        "created_at": proposal.created_at,
    }


class ConsolidationService:
    def __init__(
        self,
        *,
        session: Session,
        mem0: Any | None = None,
        source_snapshot_checker: Callable[[str, str], bool] | None = None,
        bridge_routing_ready: bool = False,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.session = session
        self.mem0 = mem0
        self.source_snapshot_checker = source_snapshot_checker or (
            lambda _project_id, _app_id: True
        )
        self.bridge_routing_ready = bridge_routing_ready
        self.now = now or (lambda: datetime.now(UTC))

    def run_scan(self, run_id: str) -> dict[str, object]:
        runs = ConsolidationRunRepository(self.session)
        policies = ConsolidationPolicyRepository(self.session)
        proposals = ConsolidationProposalRepository(self.session)
        memories = MemoryIndexRepository(self.session)
        run = runs.get(run_id)
        if run.status == "SUCCEEDED":
            return {
                "run_id": run.id,
                "status": run.status,
                "proposal_count": proposals.count_for_run(run.id),
            }

        scan_cutoff = self.now()
        runs.mark_running(run.id, scan_cutoff=scan_cutoff)
        if not self.source_snapshot_checker(run.project_id, run.app_id):
            runs.mark_failed(
                run.id,
                error_code="INCOMPLETE_SOURCE",
                error={"error_type": "IncompleteSourceSnapshot"},
                completed_at=self.now(),
            )
            return {"run_id": run.id, "status": "FAILED", "proposal_count": 0}

        policy_row = policies.get(run.project_id, run.app_id)
        if policy_row is None:
            runs.mark_failed(
                run.id,
                error_code="POLICY_NOT_FOUND",
                error={"error_type": "PolicyNotFound"},
                completed_at=self.now(),
            )
            return {"run_id": run.id, "status": "FAILED", "proposal_count": 0}

        try:
            policy = policies.spec(policy_row)
            anchors = memories.list_dirty_anchors(
                project_id=run.project_id,
                app_id=run.app_id,
                limit=policy.max_anchors_per_run,
            )
            expanded = {}
            for anchor in anchors:
                expanded[anchor.mem0_memory_id] = anchor
                for peer in memories.list_exact_group_peers(anchor, limit=100):
                    expanded[peer.mem0_memory_id] = peer
            drafts = ConsolidationAnalyzer().analyze_scope(
                run.project_id,
                run.app_id,
                policy,
                list(expanded.values()),
                scan_cutoff,
            )
            for draft in drafts:
                status = (
                    "PENDING"
                    if draft.evidence.get("safe_action") is True
                    else "REVIEW_REQUIRED"
                )
                proposals.create(
                    run=run,
                    proposal_key=draft.proposal_key,
                    kind=draft.kind,
                    source_ids=draft.source_ids,
                    canonical_id=draft.canonical_id,
                    score=draft.score,
                    evidence=draft.evidence,
                    status=status,
                )
            counts = proposals.summary_for_run(run.id)
            runs.mark_succeeded(
                run.id,
                counts=counts,
                completed_at=self.now(),
            )
            return {
                "run_id": run.id,
                "status": "SUCCEEDED",
                "proposal_count": counts["total"],
            }
        except Exception as exc:
            runs.mark_failed(
                run.id,
                error_code="SCAN_FAILED",
                error={"error_type": type(exc).__name__},
                completed_at=self.now(),
            )
            raise

    def get_status(self, project_id: str, app_id: str) -> dict[str, object]:
        policies = ConsolidationPolicyRepository(self.session)
        runs = ConsolidationRunRepository(self.session)
        proposals = ConsolidationProposalRepository(self.session)
        policy_row = policies.get(project_id, app_id)
        policy = (
            policies.spec(policy_row)
            if policy_row is not None
            else ConsolidationPolicySpec.from_mapping({})
        )
        current = runs.active(project_id, app_id)
        last = runs.last_succeeded(project_id, app_id)
        dirty_count = MemoryIndexRepository(self.session).count_dirty_anchors(
            project_id=project_id,
            app_id=app_id,
        )
        return {
            "project_id": project_id,
            "app_id": app_id,
            "policy": policy.to_mapping(),
            "configured": policy_row is not None,
            "dirty_count": dirty_count,
            "current_run": _run_payload(current) if current else None,
            "last_run": _run_payload(last) if last else None,
            "proposal_counts": proposals.counts_for_scope(project_id, app_id),
            "bridge_routing_ready": self.bridge_routing_ready,
        }

    def get_run(
        self, project_id: str, app_id: str, run_id: str
    ) -> dict[str, object]:
        return _run_payload(
            ConsolidationRunRepository(self.session).get(
                run_id, project_id=project_id, app_id=app_id
            )
        )

    def list_proposals(
        self,
        *,
        project_id: str,
        app_id: str,
        run_id: str,
        page: int,
        page_size: int,
    ) -> dict[str, object]:
        ConsolidationRunRepository(self.session).get(
            run_id, project_id=project_id, app_id=app_id
        )
        repository = ConsolidationProposalRepository(self.session)
        items = repository.list_for_run(
            project_id=project_id,
            app_id=app_id,
            run_id=run_id,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        return {
            "results": [_proposal_payload(item) for item in items],
            "page": page,
            "page_size": page_size,
            "total": repository.count_for_run(run_id),
        }
