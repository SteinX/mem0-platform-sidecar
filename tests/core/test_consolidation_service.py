import json
from datetime import UTC, datetime, timedelta

import pytest

from mem0_sidecar.core.consolidation_service import (
    ConsolidationConflictError,
    ConsolidationService,
)
from mem0_sidecar.core.memory_ops import memory_content_fingerprint
from mem0_sidecar.mem0_client.client import Mem0UpstreamError
from mem0_sidecar.store.models import (
    ConsolidationLineage,
    ConsolidationProposal,
    MemoryIndex,
)
from mem0_sidecar.store.repositories import (
    ConsolidationPolicyRepository,
    ConsolidationRunRepository,
    MemoryIndexRepository,
    ProjectRepository,
)

NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)
SAME_HASH = memory_content_fingerprint({"memory": "same"})[0]


class StatefulMem0:
    def __init__(self) -> None:
        self.memories = {
            "duplicate-a": {"id": "duplicate-a", "memory": "same"},
            "duplicate-b": {"id": "duplicate-b", "memory": "same"},
        }
        self.get_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.missing_on_get_number: dict[str, int] = {}
        self.keep_after_delete = False

    async def get_memory(self, memory_id: str):
        self.get_calls.append(memory_id)
        expected_missing_call = self.missing_on_get_number.get(memory_id)
        matching_calls = self.get_calls.count(memory_id)
        if expected_missing_call == matching_calls or memory_id not in self.memories:
            raise Mem0UpstreamError(
                method="GET",
                path=f"/memories/{memory_id}",
                status_code=404,
                response_text="not found",
                message="not found",
            )
        return self.memories[memory_id]

    async def delete_memory(self, memory_id: str):
        self.delete_calls.append(memory_id)
        if not self.keep_after_delete:
            self.memories.pop(memory_id, None)
        return {"id": memory_id, "deleted": True}


class WriteRejectingMem0:
    def __getattr__(self, name: str):
        if name in {"add_memory", "update_memory", "delete_memory"}:
            raise AssertionError(f"OBSERVE scan accessed upstream write {name}")
        raise AttributeError(name)


def seed_scope(
    db_session,
    *,
    app_id: str = "app-a",
    mode: str = "OBSERVE",
):
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    policy = ConsolidationPolicyRepository(db_session).upsert(
        project_id="repo-a",
        app_id=app_id,
        policy={"enabled": True, "mode": mode},
    )
    memories = MemoryIndexRepository(db_session)
    for memory_id in ("duplicate-a", "duplicate-b"):
        memories.upsert_memory(
            project_id="repo-a",
            mem0_memory_id=memory_id,
            user_id="root",
            app_id=app_id,
            category="decision",
            content_hash=SAME_HASH,
            content_length=20,
            normalized_type="decision",
            source="manual",
            pinned=False,
            observed_at=NOW,
        )
    run = ConsolidationRunRepository(db_session).create(policy, now=NOW)
    db_session.commit()
    return run


def seed_pending_proposal(db_session):
    run = seed_scope(db_session, mode="MANUAL")
    service = ConsolidationService(session=db_session, now=lambda: NOW)
    service.run_scan(run.id)
    db_session.commit()
    return db_session.query(ConsolidationProposal).one()


def expected_hashes() -> dict[str, str]:
    assert SAME_HASH is not None
    return {"duplicate-a": SAME_HASH, "duplicate-b": SAME_HASH}


def test_observe_scan_creates_stable_proposals_without_upstream_write(
    db_session,
) -> None:
    run = seed_scope(db_session)
    service = ConsolidationService(
        session=db_session,
        mem0=WriteRejectingMem0(),
        source_snapshot_checker=lambda _project, _app: True,
        now=lambda: NOW,
    )

    result = service.run_scan(run.id)
    db_session.commit()

    db_session.expire_all()
    persisted_run = ConsolidationRunRepository(db_session).get(run.id)
    proposals = db_session.query(ConsolidationProposal).all()
    assert result["status"] == "SUCCEEDED"
    assert persisted_run.status == "SUCCEEDED"
    assert json.loads(persisted_run.counts_json) == {
        "by_kind": {"EXACT_DUPLICATE": 1},
        "by_status": {"PENDING": 1},
        "total": 1,
    }
    assert len(proposals) == 1
    assert json.loads(proposals[0].source_ids_json) == [
        "duplicate-a",
        "duplicate-b",
    ]
    assert "memory" not in json.loads(proposals[0].evidence_json)


def test_incomplete_source_fails_scan_without_proposals(db_session) -> None:
    run = seed_scope(db_session)
    service = ConsolidationService(
        session=db_session,
        mem0=WriteRejectingMem0(),
        source_snapshot_checker=lambda _project, _app: False,
        now=lambda: NOW,
    )

    result = service.run_scan(run.id)
    db_session.commit()

    db_session.expire_all()
    persisted_run = ConsolidationRunRepository(db_session).get(run.id)
    assert result == {"run_id": run.id, "status": "FAILED", "proposal_count": 0}
    assert persisted_run.status == "FAILED"
    assert persisted_run.error_code == "INCOMPLETE_SOURCE"
    assert db_session.query(ConsolidationProposal).count() == 0


def test_scan_expands_dirty_anchor_to_clean_exact_peer(db_session) -> None:
    run = seed_scope(db_session)
    clean_peer = db_session.query(MemoryIndex).filter_by(
        mem0_memory_id="duplicate-b"
    ).one()
    clean_peer.last_observed_at = None
    db_session.commit()

    ConsolidationService(
        session=db_session,
        mem0=WriteRejectingMem0(),
        source_snapshot_checker=lambda _project, _app: True,
        now=lambda: NOW,
    ).run_scan(run.id)

    proposal = db_session.query(ConsolidationProposal).one()
    assert json.loads(proposal.source_ids_json) == [
        "duplicate-a",
        "duplicate-b",
    ]


@pytest.mark.parametrize("change", ["pinned", "hash"])
def test_approval_marks_changed_or_pinned_proposal_stale_without_upstream_write(
    db_session,
    change: str,
) -> None:
    proposal = seed_pending_proposal(db_session)
    source = db_session.query(MemoryIndex).filter_by(
        mem0_memory_id="duplicate-b"
    ).one()
    if change == "pinned":
        source.pinned = 1
    else:
        source.content_hash = "changed"
    db_session.commit()
    mem0 = StatefulMem0()

    result = ConsolidationService(
        session=db_session,
        mem0=mem0,
        bridge_routing_ready=True,
        now=lambda: NOW,
    ).approve_proposal(
        proposal.id,
        expected_status="PENDING",
        expected_source_hashes=expected_hashes(),
    )

    assert result["status"] == "STALE"
    assert mem0.get_calls == []
    assert mem0.delete_calls == []


@pytest.mark.asyncio
async def test_incomplete_export_blocks_shadowing(db_session) -> None:
    proposal = seed_pending_proposal(db_session)
    mem0 = StatefulMem0()
    service = ConsolidationService(
        session=db_session,
        mem0=mem0,
        bridge_routing_ready=True,
        now=lambda: NOW,
    )
    service.approve_proposal(
        proposal.id,
        expected_status="PENDING",
        expected_source_hashes=expected_hashes(),
    )
    db_session.commit()
    # First read verifies the source snapshot; the export's second read is missing.
    mem0.missing_on_get_number["duplicate-b"] = 2

    result = await service.shadow_approved(proposal.id)

    db_session.expire_all()
    assert result["status"] == "STALE"
    assert all(
        item.consolidation_state == "ACTIVE"
        for item in db_session.query(MemoryIndex).all()
    )
    assert mem0.delete_calls == []


@pytest.mark.asyncio
async def test_shadow_exact_duplicate_excludes_canonical_and_rolls_back(db_session):
    proposal = seed_pending_proposal(db_session)
    mem0 = StatefulMem0()
    service = ConsolidationService(
        session=db_session,
        mem0=mem0,
        bridge_routing_ready=True,
        now=lambda: NOW,
    )
    service.approve_proposal(
        proposal.id,
        expected_status="PENDING",
        expected_source_hashes=expected_hashes(),
    )

    shadowed = await service.shadow_approved(proposal.id)
    db_session.commit()

    canonical = MemoryIndexRepository(db_session).get_memory(
        project_id="repo-a", mem0_memory_id="duplicate-a", app_id="app-a"
    )
    redundant = MemoryIndexRepository(db_session).get_memory(
        project_id="repo-a", mem0_memory_id="duplicate-b", app_id="app-a"
    )
    assert shadowed["status"] == "SHADOWED"
    assert canonical is not None and canonical.consolidation_state == "ACTIVE"
    assert redundant is not None and redundant.consolidation_state == "SHADOWED"
    repository = MemoryIndexRepository(db_session)
    assert repository.list_scoped_memory_ids(
        project_id="repo-a",
        mem0_memory_ids=["duplicate-a", "duplicate-b"],
        user_id=None,
        app_id="app-a",
        agent_id=None,
        run_id=None,
    ) == {"duplicate-a"}
    rollback_source = repository.get_memory(
        project_id="repo-a",
        mem0_memory_id="duplicate-b",
        app_id="app-a",
    )
    assert rollback_source is not None
    assert rollback_source.consolidation_state == "SHADOWED"

    rolled_back = service.rollback_shadowed(proposal.id)

    assert rolled_back["status"] == "ROLLED_BACK"
    assert repository.list_scoped_memory_ids(
        project_id="repo-a",
        mem0_memory_ids=["duplicate-a", "duplicate-b"],
        user_id=None,
        app_id="app-a",
        agent_id=None,
        run_id=None,
    ) == {"duplicate-a", "duplicate-b"}
    assert mem0.delete_calls == []


@pytest.mark.asyncio
async def test_finalize_enforces_grace_and_hard_delete_gate(db_session) -> None:
    proposal = seed_pending_proposal(db_session)
    mem0 = StatefulMem0()
    service = ConsolidationService(
        session=db_session,
        mem0=mem0,
        bridge_routing_ready=True,
        now=lambda: NOW,
    )
    service.approve_proposal(
        proposal.id,
        expected_status="PENDING",
        expected_source_hashes=expected_hashes(),
    )
    await service.shadow_approved(proposal.id)

    with pytest.raises(ConsolidationConflictError, match="grace"):
        await service.finalize_shadowed(proposal.id, now=NOW)

    after_grace = NOW + timedelta(days=8)
    gated = await service.finalize_shadowed(proposal.id, now=after_grace)
    assert gated == {"proposal_id": proposal.id, "status": "SHADOWED"}
    assert mem0.delete_calls == []


@pytest.mark.asyncio
async def test_finalize_deletes_redundant_one_at_a_time_and_records_lineage(
    db_session,
) -> None:
    proposal = seed_pending_proposal(db_session)
    mem0 = StatefulMem0()
    service = ConsolidationService(
        session=db_session,
        mem0=mem0,
        bridge_routing_ready=True,
        hard_delete_enabled=True,
        now=lambda: NOW,
    )
    service.approve_proposal(
        proposal.id,
        expected_status="PENDING",
        expected_source_hashes=expected_hashes(),
    )
    await service.shadow_approved(proposal.id)

    applied = await service.finalize_shadowed(
        proposal.id,
        now=NOW + timedelta(days=8),
    )

    assert applied["status"] == "APPLIED"
    assert mem0.delete_calls == ["duplicate-b"]
    assert "duplicate-a" in mem0.memories
    lineage = db_session.query(ConsolidationLineage).one()
    assert lineage.source_memory_id == "duplicate-b"
    assert lineage.canonical_memory_id == "duplicate-a"
    assert lineage.action == "EXACT_DUPLICATE_DELETE"
    assert lineage.export_job_id is not None


@pytest.mark.asyncio
async def test_ambiguous_delete_never_marks_proposal_applied(db_session) -> None:
    proposal = seed_pending_proposal(db_session)
    mem0 = StatefulMem0()
    mem0.keep_after_delete = True
    service = ConsolidationService(
        session=db_session,
        mem0=mem0,
        bridge_routing_ready=True,
        hard_delete_enabled=True,
        now=lambda: NOW,
    )
    service.approve_proposal(
        proposal.id,
        expected_status="PENDING",
        expected_source_hashes=expected_hashes(),
    )
    await service.shadow_approved(proposal.id)

    with pytest.raises(ConsolidationConflictError, match="verification"):
        await service.finalize_shadowed(
            proposal.id,
            now=NOW + timedelta(days=8),
        )

    db_session.expire_all()
    assert db_session.get(ConsolidationProposal, proposal.id).status in {
        "FAILED",
        "SHADOWED",
    }
    assert db_session.get(ConsolidationProposal, proposal.id).status != "APPLIED"
