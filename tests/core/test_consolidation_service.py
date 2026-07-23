import json
from datetime import UTC, datetime, timedelta

import pytest

from mem0_sidecar.core.consolidation_service import (
    ConsolidationConflictError,
    ConsolidationService,
)
from mem0_sidecar.core.memory_ops import memory_content_fingerprint
from mem0_sidecar.mem0_client.client import Mem0UpstreamError
from mem0_sidecar.store.database import create_session_factory
from mem0_sidecar.store.models import (
    ConsolidationLineage,
    ConsolidationProposal,
    MemoryIndex,
)
from mem0_sidecar.store.repositories import (
    ConsolidationPolicyRepository,
    ConsolidationProposalRepository,
    ConsolidationRunRepository,
    MemoryIndexRepository,
    ProjectRepository,
)

NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)
SAME_HASH = memory_content_fingerprint({"memory": "same"})[0]


class StatefulMem0:
    def __init__(self) -> None:
        self.memories = {
            "duplicate-a": {
                "id": "duplicate-a",
                "memory": "same",
                "metadata": {
                    "_mem0_sidecar_project_id": "repo-a",
                    "_mem0_sidecar_app_id": "app-a",
                },
            },
            "duplicate-b": {
                "id": "duplicate-b",
                "memory": "same",
                "metadata": {
                    "_mem0_sidecar_project_id": "repo-a",
                    "_mem0_sidecar_app_id": "app-a",
                },
            },
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


class ReentrantMem0(StatefulMem0):
    def __init__(self, on_first_get) -> None:
        super().__init__()
        self.on_first_get = on_first_get
        self.reentered = False

    async def get_memory(self, memory_id: str):
        if not self.reentered:
            self.reentered = True
            await self.on_first_get()
        return await super().get_memory(memory_id)


class SemanticMem0:
    def __init__(self) -> None:
        self.records = {
            "near-a": {
                "id": "near-a",
                "memory": "Use PostgreSQL as the primary production datastore",
            },
            "near-b": {
                "id": "near-b",
                "memory": "Use PostgreSQL for the primary production data store",
            },
            "low": {"id": "low", "memory": "PostgreSQL note from an old task"},
            "cross-app": {
                "id": "cross-app",
                "memory": "Use PostgreSQL as the primary production datastore",
            },
        }
        self.search_payloads: list[dict[str, object]] = []
        self.get_calls: list[str] = []

    async def get_memory(self, memory_id: str):
        self.get_calls.append(memory_id)
        return self.records[memory_id]

    async def search_memories(self, payload: dict[str, object]):
        self.search_payloads.append(payload)
        if "app_id" in payload:
            raise AssertionError("raw OSS search received unsupported app_id")
        filters = payload.get("filters")
        if isinstance(filters, dict) and "normalized_type" in filters:
            raise AssertionError(
                "raw OSS search filtered on projection-only normalized_type"
            )
        query = payload["query"]
        if query == self.records["near-a"]["memory"]:
            return {
                "results": [
                    {"id": "near-b", "score": 0.95},
                    {"id": "low", "score": 0.80},
                    {"id": "cross-app", "score": 0.99},
                ]
            }
        if query == self.records["near-b"]["memory"]:
            return {"results": [{"id": "near-a", "score": 0.95}]}
        return {"results": []}


class ReplacementMem0:
    def __init__(self) -> None:
        self.add_payloads: list[dict[str, object]] = []
        self.delete_calls: list[str] = []
        self.mutate_replacement_during_export = False
        self.semantic_a_gets = 0
        scope_metadata = {
            "_mem0_sidecar_project_id": "repo-a",
            "_mem0_sidecar_app_id": "app-a",
        }
        self.records = {
            "semantic-a": {
                "id": "semantic-a",
                "memory": "use v1",
                "metadata": dict(scope_metadata),
            },
            "semantic-b": {
                "id": "semantic-b",
                "memory": "use v2",
                "metadata": dict(scope_metadata),
            },
        }

    async def add_memory(self, payload: dict[str, object]):
        self.add_payloads.append(payload)
        content = payload["text"]
        record = {
            "id": "replacement",
            "memory": content,
            "metadata": payload.get("metadata", {}),
        }
        self.records["replacement"] = record
        return record

    async def get_memory(self, memory_id: str):
        if memory_id not in self.records:
            raise Mem0UpstreamError(
                method="GET",
                path=f"/memories/{memory_id}",
                status_code=404,
                response_text="not found",
                message="not found",
            )
        if memory_id == "semantic-a":
            self.semantic_a_gets += 1
            if (
                self.mutate_replacement_during_export
                and self.semantic_a_gets == 2
            ):
                self.records["replacement"]["memory"] = "raced replacement"
        return self.records[memory_id]

    async def delete_memory(self, memory_id: str):
        self.delete_calls.append(memory_id)
        self.records.pop(memory_id, None)
        return {"id": memory_id, "deleted": True}


class ScopeBackfillMem0:
    def __init__(self) -> None:
        self.records = {
            "legacy": {
                "id": "legacy",
                "memory": "legacy memory",
                "metadata": {"type": "decision"},
            }
        }
        self.update_calls: list[tuple[str, dict[str, object]]] = []

    async def get_memory(self, memory_id: str):
        return self.records[memory_id]

    async def update_memory(self, memory_id: str, payload: dict[str, object]):
        self.update_calls.append((memory_id, payload))
        metadata = payload.get("metadata")
        assert isinstance(metadata, dict)
        self.records[memory_id]["metadata"] = metadata
        return {"id": memory_id, "updated": True}


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


async def seed_pending_proposal(db_session):
    run = seed_scope(db_session, mode="MANUAL")
    service = ConsolidationService(session=db_session, now=lambda: NOW)
    await service.run_scan(run.id)
    db_session.commit()
    return db_session.query(ConsolidationProposal).one()


def expected_hashes() -> dict[str, str]:
    assert SAME_HASH is not None
    return {"duplicate-a": SAME_HASH, "duplicate-b": SAME_HASH}


@pytest.mark.asyncio
async def test_observe_scan_creates_stable_proposals_without_upstream_write(
    db_session,
) -> None:
    run = seed_scope(db_session)
    service = ConsolidationService(
        session=db_session,
        mem0=WriteRejectingMem0(),
        source_snapshot_checker=lambda _project, _app: True,
        now=lambda: NOW,
    )

    result = await service.run_scan(run.id)
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


@pytest.mark.asyncio
async def test_incomplete_source_fails_scan_without_proposals(db_session) -> None:
    run = seed_scope(db_session)
    service = ConsolidationService(
        session=db_session,
        mem0=WriteRejectingMem0(),
        source_snapshot_checker=lambda _project, _app: False,
        now=lambda: NOW,
    )

    result = await service.run_scan(run.id)
    db_session.commit()

    db_session.expire_all()
    persisted_run = ConsolidationRunRepository(db_session).get(run.id)
    assert result == {"run_id": run.id, "status": "FAILED", "proposal_count": 0}
    assert persisted_run.status == "FAILED"
    assert persisted_run.error_code == "INCOMPLETE_SOURCE"
    assert db_session.query(ConsolidationProposal).count() == 0


@pytest.mark.asyncio
async def test_successful_scan_advances_past_clean_anchor_batch(db_session) -> None:
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
            "max_anchors_per_run": 1,
        },
    )
    memories = MemoryIndexRepository(db_session)
    for offset, memory_id in enumerate(("clean-a", "clean-b")):
        content_hash, length = memory_content_fingerprint(
            {"memory": f"unique-{memory_id}"}
        )
        memories.upsert_memory(
            project_id="repo-a",
            mem0_memory_id=memory_id,
            user_id="root",
            app_id="app-a",
            category="decision",
            content_hash=content_hash,
            content_length=length,
            normalized_type="decision",
            source="manual",
            pinned=False,
            observed_at=NOW + timedelta(seconds=offset),
        )
    first_run = ConsolidationRunRepository(db_session).create(policy, now=NOW)
    db_session.commit()

    service = ConsolidationService(
        session=db_session,
        mem0=WriteRejectingMem0(),
        now=lambda: NOW + timedelta(seconds=2),
    )
    await service.run_scan(first_run.id)

    remaining = memories.list_dirty_anchors(
        project_id="repo-a",
        app_id="app-a",
        limit=10,
    )
    assert [memory.mem0_memory_id for memory in remaining] == ["clean-b"]


@pytest.mark.asyncio
async def test_scan_does_not_consume_anchor_updated_after_cutoff(db_session) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    policy = ConsolidationPolicyRepository(db_session).upsert(
        project_id="repo-a",
        app_id="app-a",
        policy={"enabled": True, "mode": "OBSERVE"},
    )
    content_hash, length = memory_content_fingerprint(
        {"memory": "unique-clean-memory"}
    )
    memories = MemoryIndexRepository(db_session)
    memories.upsert_memory(
        project_id="repo-a",
        mem0_memory_id="clean-a",
        user_id="root",
        app_id="app-a",
        category="decision",
        content_hash=content_hash,
        content_length=length,
        normalized_type="decision",
        source="manual",
        pinned=False,
        observed_at=NOW,
    )
    run = ConsolidationRunRepository(db_session).create(policy, now=NOW)
    db_session.commit()

    class UpdatingMem0:
        async def get_memory(self, memory_id: str):
            return {"id": memory_id, "memory": "unique-clean-memory"}

        async def search_memories(self, _payload):
            memory = memories.get_memory(
                project_id="repo-a",
                mem0_memory_id="clean-a",
                app_id="app-a",
            )
            assert memory is not None
            memory.last_observed_at = NOW + timedelta(seconds=1)
            db_session.commit()
            return {"results": []}

    await ConsolidationService(
        session=db_session,
        mem0=UpdatingMem0(),
        now=lambda: NOW,
    ).run_scan(run.id)

    remaining = memories.list_dirty_anchors(
        project_id="repo-a",
        app_id="app-a",
        limit=10,
    )
    assert [memory.mem0_memory_id for memory in remaining] == ["clean-a"]


@pytest.mark.asyncio
async def test_scan_expands_dirty_anchor_to_clean_exact_peer(db_session) -> None:
    run = seed_scope(db_session)
    clean_peer = db_session.query(MemoryIndex).filter_by(
        mem0_memory_id="duplicate-b"
    ).one()
    clean_peer.last_observed_at = None
    db_session.commit()

    await ConsolidationService(
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


@pytest.mark.asyncio
async def test_scan_adds_bounded_scoped_semantic_pair_once(db_session) -> None:
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
            "near_duplicate_threshold": 0.92,
        },
    )
    mem0 = SemanticMem0()
    memories = MemoryIndexRepository(db_session)
    for memory_id in ("near-a", "near-b", "low"):
        content_hash, length = memory_content_fingerprint(mem0.records[memory_id])
        memories.upsert_memory(
            project_id="repo-a",
            mem0_memory_id=memory_id,
            user_id="root",
            app_id="app-a",
            category="decision",
            content_hash=content_hash,
            content_length=length,
            normalized_type="decision",
            source="manual",
            pinned=False,
            observed_at=NOW,
        )
    cross_hash, cross_length = memory_content_fingerprint(
        mem0.records["cross-app"]
    )
    memories.upsert_memory(
        project_id="repo-a",
        mem0_memory_id="cross-app",
        user_id="root",
        app_id="app-b",
        category="decision",
        content_hash=cross_hash,
        content_length=cross_length,
        normalized_type="decision",
        source="manual",
        pinned=False,
        observed_at=NOW,
    )
    run = ConsolidationRunRepository(db_session).create(policy, now=NOW)
    db_session.commit()

    result = await ConsolidationService(
        session=db_session,
        mem0=mem0,
        now=lambda: NOW,
    ).run_scan(run.id)

    proposals = db_session.query(ConsolidationProposal).all()
    assert result == {"run_id": run.id, "status": "SUCCEEDED", "proposal_count": 1}
    assert [(item.kind, item.status) for item in proposals] == [
        ("NEAR_DUPLICATE", "REVIEW_REQUIRED")
    ]
    assert json.loads(proposals[0].source_ids_json) == ["near-a", "near-b"]
    assert json.loads(proposals[0].evidence_json)["safe_action"] is False
    assert len(mem0.search_payloads) == 3
    assert mem0.search_payloads[0]["top_k"] == 10
    assert mem0.search_payloads[0]["threshold"] == 0.92
    assert "app_id" not in mem0.search_payloads[0]
    assert mem0.search_payloads[0]["user_id"] == "root"
    assert mem0.search_payloads[0]["filters"] == {
        "_mem0_sidecar_app_id": "app-a",
        "_mem0_sidecar_project_id": "repo-a",
    }


@pytest.mark.asyncio
async def test_scope_marker_backfill_is_explicit_audited_and_counted(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    ConsolidationPolicyRepository(db_session).upsert(
        project_id="repo-a",
        app_id="app-a",
        policy={"enabled": True, "mode": "OBSERVE"},
    )
    content_hash, length = memory_content_fingerprint(
        {"memory": "legacy memory"}
    )
    MemoryIndexRepository(db_session).upsert_memory(
        project_id="repo-a",
        mem0_memory_id="legacy",
        user_id="root",
        app_id="app-a",
        category="decision",
        metadata={"type": "decision"},
        content_hash=content_hash,
        content_length=length,
        normalized_type="decision",
        source="legacy",
        pinned=False,
        scope_markers_verified=False,
        observed_at=NOW,
    )
    db_session.commit()
    mem0 = ScopeBackfillMem0()
    service = ConsolidationService(
        session=db_session,
        mem0=mem0,
        bridge_routing_ready=True,
        scope_backfill_writes_paused=True,
        now=lambda: NOW,
    )

    assert service.get_status("repo-a", "app-a")[
        "scope_marker_backfill_required"
    ] == 1
    assert MemoryIndexRepository(db_session).count_dirty_anchors(
        project_id="repo-a",
        app_id="app-a",
    ) == 0
    result = await service.backfill_scope_markers(
        project_id="repo-a",
        app_id="app-a",
        limit=10,
    )

    assert result == {
        "scanned": 1,
        "backfilled": 1,
        "already_scoped": 0,
        "skipped_conflict": 0,
        "missing": 0,
        "remaining": 0,
        "unresolved": {},
    }
    assert mem0.update_calls[0][0] == "legacy"
    assert mem0.records["legacy"]["metadata"] == {
        "type": "decision",
        "_mem0_sidecar_project_id": "repo-a",
        "_mem0_sidecar_app_id": "app-a",
    }
    projection = MemoryIndexRepository(db_session).get_memory(
        project_id="repo-a",
        mem0_memory_id="legacy",
        app_id="app-a",
    )
    assert projection is not None and projection.scope_markers_verified == 1
    assert service.get_status("repo-a", "app-a")[
        "scope_marker_backfill_required"
    ] == 0


@pytest.mark.parametrize(
    ("bridge_ready", "writes_paused", "message"),
    [
        (False, True, "bridge routing"),
        (True, False, "write pause"),
    ],
)
@pytest.mark.asyncio
async def test_scope_backfill_requires_full_routing_and_write_pause(
    db_session,
    bridge_ready: bool,
    writes_paused: bool,
    message: str,
) -> None:
    with pytest.raises(ConsolidationConflictError, match=message):
        await ConsolidationService(
            session=db_session,
            mem0=ScopeBackfillMem0(),
            bridge_routing_ready=bridge_ready,
            scope_backfill_writes_paused=writes_paused,
            now=lambda: NOW,
        ).backfill_scope_markers(
            project_id="repo-a",
            app_id="app-a",
            limit=10,
        )


@pytest.mark.asyncio
async def test_scope_backfill_persists_unresolved_outcome_and_advances_batch(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    for memory_id in ("conflict", "later"):
        content_hash, length = memory_content_fingerprint(
            {"memory": memory_id}
        )
        MemoryIndexRepository(db_session).upsert_memory(
            project_id="repo-a",
            mem0_memory_id=memory_id,
            user_id="root",
            app_id="app-a",
            category="decision",
            metadata={},
            content_hash=content_hash,
            content_length=length,
            normalized_type="decision",
            source="legacy",
            pinned=False,
            scope_markers_verified=False,
            observed_at=NOW,
        )
    db_session.commit()

    class PagedBackfillMem0:
        def __init__(self) -> None:
            self.records = {
                "conflict": {
                    "id": "conflict",
                    "memory": "conflict",
                    "metadata": {
                        "_mem0_sidecar_project_id": "repo-b",
                        "_mem0_sidecar_app_id": "app-b",
                    },
                },
                "later": {"id": "later", "memory": "later", "metadata": {}},
            }

        async def get_memory(self, memory_id: str):
            return self.records[memory_id]

        async def update_memory(self, memory_id: str, payload):
            self.records[memory_id]["metadata"] = payload["metadata"]
            return {"id": memory_id, "updated": True}

    service = ConsolidationService(
        session=db_session,
        mem0=PagedBackfillMem0(),
        bridge_routing_ready=True,
        scope_backfill_writes_paused=True,
        now=lambda: NOW,
    )

    first = await service.backfill_scope_markers(
        project_id="repo-a",
        app_id="app-a",
        limit=1,
    )
    second = await service.backfill_scope_markers(
        project_id="repo-a",
        app_id="app-a",
        limit=1,
    )

    assert first["skipped_conflict"] == 1
    assert second["backfilled"] == 1
    assert second["remaining"] == 1
    conflict = MemoryIndexRepository(db_session).get_memory(
        project_id="repo-a",
        mem0_memory_id="conflict",
        app_id="app-a",
    )
    assert conflict is not None
    assert conflict.scope_marker_backfill_status == "CONFLICT"
    assert conflict.scope_marker_backfill_attempted_at is not None


@pytest.mark.parametrize("change", ["pinned", "hash"])
@pytest.mark.asyncio
async def test_approval_marks_changed_or_pinned_proposal_stale_without_upstream_write(
    db_session,
    change: str,
) -> None:
    proposal = await seed_pending_proposal(db_session)
    source = db_session.query(MemoryIndex).filter_by(
        mem0_memory_id="duplicate-b"
    ).one()
    if change == "pinned":
        source.pinned = 1
    else:
        source.content_hash = "changed"
    db_session.commit()
    mem0 = StatefulMem0()

    result = await ConsolidationService(
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
async def test_manual_semantic_approval_can_create_text_free_replacement_decision(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    policy = ConsolidationPolicyRepository(db_session).upsert(
        project_id="repo-a",
        app_id="app-a",
        policy={"enabled": True, "mode": "MANUAL"},
    )
    source_texts = {"semantic-a": "use v1", "semantic-b": "use v2"}
    hashes: dict[str, str] = {}
    for memory_id, text_value in source_texts.items():
        content_hash, length = memory_content_fingerprint({"memory": text_value})
        assert content_hash is not None
        hashes[memory_id] = content_hash
        MemoryIndexRepository(db_session).upsert_memory(
            project_id="repo-a",
            mem0_memory_id=memory_id,
            user_id="root",
            app_id="app-a",
            category="decision",
            content_hash=content_hash,
            content_length=length,
            normalized_type="decision",
            source="manual",
            pinned=False,
            observed_at=NOW,
        )
    run = ConsolidationRunRepository(db_session).create(policy, now=NOW)
    run.status = "SUCCEEDED"
    proposal = ConsolidationProposalRepository(db_session).create(
        run=run,
        proposal_key="semantic-key",
        kind="CONTRADICTION",
        source_ids=("semantic-a", "semantic-b"),
        canonical_id=None,
        score=0.97,
        evidence={"reason_codes": ["version_change"], "safe_action": False},
        status="REVIEW_REQUIRED",
    )
    db_session.commit()
    mem0 = ReplacementMem0()
    replacement = "use v3"

    result = await ConsolidationService(
        session=db_session,
        mem0=mem0,
        bridge_routing_ready=True,
        now=lambda: NOW,
    ).approve_proposal(
        proposal.id,
        expected_status="REVIEW_REQUIRED",
        expected_source_hashes=hashes,
        replacement_text=replacement,
    )

    db_session.expire_all()
    persisted = db_session.get(ConsolidationProposal, proposal.id)
    assert result["status"] == "APPROVED"
    assert persisted.canonical_memory_id == "replacement"
    assert replacement not in persisted.evidence_json
    assert json.loads(persisted.evidence_json)["operator_decision"] == (
        "replacement_created"
    )


@pytest.mark.parametrize("change", ["missing", "hash", "scope"])
@pytest.mark.asyncio
async def test_finalize_revalidates_semantic_replacement_canonical(
    db_session,
    change: str,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    policy = ConsolidationPolicyRepository(db_session).upsert(
        project_id="repo-a",
        app_id="app-a",
        policy={"enabled": True, "mode": "MANUAL"},
    )
    hashes: dict[str, str] = {}
    for memory_id, text_value in {
        "semantic-a": "use v1",
        "semantic-b": "use v2",
    }.items():
        content_hash, length = memory_content_fingerprint({"memory": text_value})
        assert content_hash is not None
        hashes[memory_id] = content_hash
        MemoryIndexRepository(db_session).upsert_memory(
            project_id="repo-a",
            mem0_memory_id=memory_id,
            user_id="root",
            app_id="app-a",
            category="decision",
            content_hash=content_hash,
            content_length=length,
            normalized_type="decision",
            source="manual",
            pinned=False,
            observed_at=NOW,
        )
    run = ConsolidationRunRepository(db_session).create(policy, now=NOW)
    run.status = "SUCCEEDED"
    proposal = ConsolidationProposalRepository(db_session).create(
        run=run,
        proposal_key="semantic-replacement-key",
        kind="CONTRADICTION",
        source_ids=("semantic-a", "semantic-b"),
        canonical_id=None,
        score=0.97,
        evidence={"reason_codes": ["version_change"], "safe_action": False},
        status="REVIEW_REQUIRED",
    )
    db_session.commit()
    mem0 = ReplacementMem0()
    service = ConsolidationService(
        session=db_session,
        mem0=mem0,
        bridge_routing_ready=True,
        hard_delete_enabled=True,
        now=lambda: NOW,
    )
    await service.approve_proposal(
        proposal.id,
        expected_status="REVIEW_REQUIRED",
        expected_source_hashes=hashes,
        replacement_text="use v3",
    )
    await service.shadow_approved(proposal.id)
    if change == "missing":
        mem0.records.pop("replacement")
    elif change == "hash":
        mem0.records["replacement"]["memory"] = "changed replacement"
    else:
        mem0.records["replacement"]["metadata"][
            "_mem0_sidecar_app_id"
        ] = "app-b"

    with pytest.raises(
        ConsolidationConflictError,
        match="canonical memory changed",
    ):
        await service.finalize_shadowed(
            proposal.id,
            now=NOW + timedelta(days=8),
        )

    assert mem0.delete_calls == []


@pytest.mark.asyncio
async def test_shadow_revalidates_replacement_after_export(db_session) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    policy = ConsolidationPolicyRepository(db_session).upsert(
        project_id="repo-a",
        app_id="app-a",
        policy={"enabled": True, "mode": "MANUAL"},
    )
    hashes: dict[str, str] = {}
    for memory_id, text_value in {
        "semantic-a": "use v1",
        "semantic-b": "use v2",
    }.items():
        content_hash, length = memory_content_fingerprint({"memory": text_value})
        assert content_hash is not None
        hashes[memory_id] = content_hash
        MemoryIndexRepository(db_session).upsert_memory(
            project_id="repo-a",
            mem0_memory_id=memory_id,
            user_id="root",
            app_id="app-a",
            category="decision",
            content_hash=content_hash,
            content_length=length,
            normalized_type="decision",
            source="manual",
            pinned=False,
            observed_at=NOW,
        )
    run = ConsolidationRunRepository(db_session).create(policy, now=NOW)
    run.status = "SUCCEEDED"
    proposal = ConsolidationProposalRepository(db_session).create(
        run=run,
        proposal_key="semantic-export-race",
        kind="CONTRADICTION",
        source_ids=("semantic-a", "semantic-b"),
        canonical_id=None,
        score=0.97,
        evidence={"reason_codes": ["version_change"], "safe_action": False},
        status="REVIEW_REQUIRED",
    )
    db_session.commit()
    mem0 = ReplacementMem0()
    service = ConsolidationService(
        session=db_session,
        mem0=mem0,
        bridge_routing_ready=True,
        now=lambda: NOW,
    )
    await service.approve_proposal(
        proposal.id,
        expected_status="REVIEW_REQUIRED",
        expected_source_hashes=hashes,
        replacement_text="use v3",
    )
    mem0.mutate_replacement_during_export = True

    result = await service.shadow_approved(proposal.id)

    db_session.expire_all()
    assert result["status"] == "STALE"
    assert db_session.get(ConsolidationProposal, proposal.id).status == "STALE"
    assert {
        item.consolidation_state for item in db_session.query(MemoryIndex).all()
    } == {"ACTIVE"}


@pytest.mark.asyncio
async def test_incomplete_export_blocks_shadowing(db_session) -> None:
    proposal = await seed_pending_proposal(db_session)
    mem0 = StatefulMem0()
    service = ConsolidationService(
        session=db_session,
        mem0=mem0,
        bridge_routing_ready=True,
        now=lambda: NOW,
    )
    await service.approve_proposal(
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
    proposal = await seed_pending_proposal(db_session)
    mem0 = StatefulMem0()
    service = ConsolidationService(
        session=db_session,
        mem0=mem0,
        bridge_routing_ready=True,
        now=lambda: NOW,
    )
    await service.approve_proposal(
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
async def test_shadow_replay_does_not_downgrade_completed_proposal(db_session) -> None:
    proposal = await seed_pending_proposal(db_session)
    approval_mem0 = StatefulMem0()
    approval_service = ConsolidationService(
        session=db_session,
        mem0=approval_mem0,
        bridge_routing_ready=True,
        now=lambda: NOW,
    )
    await approval_service.approve_proposal(
        proposal.id,
        expected_status="PENDING",
        expected_source_hashes=expected_hashes(),
    )
    db_session.commit()

    factory = create_session_factory(db_session.get_bind())
    replay_results: list[dict[str, object]] = []

    async def replay_shadow() -> None:
        with factory() as replay_session:
            replay_results.append(
                await ConsolidationService(
                    session=replay_session,
                    mem0=StatefulMem0(),
                    bridge_routing_ready=True,
                    now=lambda: NOW,
                ).shadow_approved(proposal.id)
            )

    outer_result = await ConsolidationService(
        session=db_session,
        mem0=ReentrantMem0(replay_shadow),
        bridge_routing_ready=True,
        now=lambda: NOW,
    ).shadow_approved(proposal.id)

    db_session.expire_all()
    persisted = db_session.get(ConsolidationProposal, proposal.id)
    assert replay_results[0]["status"] == "EXPORTING"
    assert outer_result["status"] == "SHADOWED"
    assert persisted.status == "SHADOWED"


@pytest.mark.asyncio
async def test_concurrent_shadow_replay_cannot_override_active_owner(
    db_session,
) -> None:
    proposal = await seed_pending_proposal(db_session)
    approval_service = ConsolidationService(
        session=db_session,
        mem0=StatefulMem0(),
        bridge_routing_ready=True,
        now=lambda: NOW,
    )
    await approval_service.approve_proposal(
        proposal.id,
        expected_status="PENDING",
        expected_source_hashes=expected_hashes(),
    )
    db_session.commit()

    factory = create_session_factory(db_session.get_bind())
    replay_results: list[dict[str, object]] = []

    async def failing_replay() -> None:
        replay_mem0 = StatefulMem0()
        replay_mem0.missing_on_get_number["duplicate-b"] = 2
        with factory() as replay_session:
            replay_results.append(
                await ConsolidationService(
                    session=replay_session,
                    mem0=replay_mem0,
                    bridge_routing_ready=True,
                    now=lambda: NOW,
                ).shadow_approved(proposal.id)
            )

    owner_result = await ConsolidationService(
        session=db_session,
        mem0=ReentrantMem0(failing_replay),
        bridge_routing_ready=True,
        now=lambda: NOW,
    ).shadow_approved(proposal.id)

    db_session.expire_all()
    assert replay_results == [
        {"proposal_id": proposal.id, "status": "EXPORTING"}
    ]
    assert owner_result["status"] == "SHADOWED"
    assert db_session.get(ConsolidationProposal, proposal.id).status == "SHADOWED"


@pytest.mark.asyncio
async def test_shadow_retries_exporting_proposal_after_interruption(db_session) -> None:
    proposal = await seed_pending_proposal(db_session)
    service = ConsolidationService(
        session=db_session,
        mem0=StatefulMem0(),
        bridge_routing_ready=True,
        now=lambda: NOW,
    )
    await service.approve_proposal(
        proposal.id,
        expected_status="PENDING",
        expected_source_hashes=expected_hashes(),
    )
    proposal.status = "EXPORTING"
    db_session.commit()

    result = await service.shadow_approved(proposal.id)

    assert result["status"] == "SHADOWED"
    assert db_session.get(ConsolidationProposal, proposal.id).status == "SHADOWED"


@pytest.mark.asyncio
async def test_finalize_enforces_grace_and_hard_delete_gate(db_session) -> None:
    proposal = await seed_pending_proposal(db_session)
    mem0 = StatefulMem0()
    service = ConsolidationService(
        session=db_session,
        mem0=mem0,
        bridge_routing_ready=True,
        now=lambda: NOW,
    )
    await service.approve_proposal(
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
async def test_finalize_requires_current_bridge_routing_heartbeat(db_session) -> None:
    proposal = await seed_pending_proposal(db_session)
    mem0 = StatefulMem0()
    service = ConsolidationService(
        session=db_session,
        mem0=mem0,
        bridge_routing_ready=True,
        hard_delete_enabled=True,
        now=lambda: NOW,
    )
    await service.approve_proposal(
        proposal.id,
        expected_status="PENDING",
        expected_source_hashes=expected_hashes(),
    )
    await service.shadow_approved(proposal.id)
    service.bridge_routing_ready = False

    with pytest.raises(ConsolidationConflictError, match="bridge routing"):
        await service.finalize_shadowed(
            proposal.id,
            now=NOW + timedelta(days=8),
        )

    assert mem0.delete_calls == []


@pytest.mark.parametrize("change", ["pinned", "hash", "scope"])
@pytest.mark.asyncio
async def test_finalize_revalidates_upstream_source_before_delete(
    db_session,
    change: str,
) -> None:
    proposal = await seed_pending_proposal(db_session)
    mem0 = StatefulMem0()
    service = ConsolidationService(
        session=db_session,
        mem0=mem0,
        bridge_routing_ready=True,
        hard_delete_enabled=True,
        now=lambda: NOW,
    )
    await service.approve_proposal(
        proposal.id,
        expected_status="PENDING",
        expected_source_hashes=expected_hashes(),
    )
    await service.shadow_approved(proposal.id)
    if change == "pinned":
        mem0.memories["duplicate-b"]["metadata"]["pinned"] = True
    elif change == "hash":
        mem0.memories["duplicate-b"]["memory"] = "changed"
    else:
        mem0.memories["duplicate-b"]["metadata"][
            "_mem0_sidecar_project_id"
        ] = "repo-b"

    with pytest.raises(ConsolidationConflictError, match="upstream source changed"):
        await service.finalize_shadowed(
            proposal.id,
            now=NOW + timedelta(days=8),
        )

    db_session.expire_all()
    assert mem0.delete_calls == []
    assert db_session.get(ConsolidationProposal, proposal.id).status == "SHADOWED"


@pytest.mark.asyncio
async def test_finalize_deletes_redundant_one_at_a_time_and_records_lineage(
    db_session,
) -> None:
    proposal = await seed_pending_proposal(db_session)
    mem0 = StatefulMem0()
    service = ConsolidationService(
        session=db_session,
        mem0=mem0,
        bridge_routing_ready=True,
        hard_delete_enabled=True,
        now=lambda: NOW,
    )
    await service.approve_proposal(
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
    proposal = await seed_pending_proposal(db_session)
    mem0 = StatefulMem0()
    mem0.keep_after_delete = True
    service = ConsolidationService(
        session=db_session,
        mem0=mem0,
        bridge_routing_ready=True,
        hard_delete_enabled=True,
        now=lambda: NOW,
    )
    await service.approve_proposal(
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
