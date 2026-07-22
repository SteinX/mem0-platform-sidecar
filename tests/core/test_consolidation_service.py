import json
from datetime import UTC, datetime

from mem0_sidecar.core.consolidation_service import ConsolidationService
from mem0_sidecar.store.models import ConsolidationProposal, MemoryIndex
from mem0_sidecar.store.repositories import (
    ConsolidationPolicyRepository,
    ConsolidationRunRepository,
    MemoryIndexRepository,
    ProjectRepository,
)

NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)


class WriteRejectingMem0:
    def __getattr__(self, name: str):
        if name in {"add_memory", "update_memory", "delete_memory"}:
            raise AssertionError(f"OBSERVE scan accessed upstream write {name}")
        raise AttributeError(name)


def seed_scope(db_session, *, app_id: str = "app-a"):
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    policy = ConsolidationPolicyRepository(db_session).upsert(
        project_id="repo-a",
        app_id=app_id,
        policy={"enabled": True, "mode": "OBSERVE"},
    )
    memories = MemoryIndexRepository(db_session)
    for memory_id in ("duplicate-a", "duplicate-b"):
        memories.upsert_memory(
            project_id="repo-a",
            mem0_memory_id=memory_id,
            user_id="root",
            app_id=app_id,
            category="decision",
            content_hash="same-hash",
            content_length=20,
            normalized_type="decision",
            source="manual",
            pinned=False,
            observed_at=NOW,
        )
    run = ConsolidationRunRepository(db_session).create(policy, now=NOW)
    db_session.commit()
    return run


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
