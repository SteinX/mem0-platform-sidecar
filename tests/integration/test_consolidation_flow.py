from datetime import UTC, datetime, timedelta

import pytest

from mem0_sidecar.core.consolidation_service import ConsolidationService
from mem0_sidecar.core.memory_ops import MemoryService, memory_content_fingerprint
from mem0_sidecar.mem0_client.client import Mem0UpstreamError
from mem0_sidecar.store.models import ConsolidationLineage, ConsolidationProposal
from mem0_sidecar.store.repositories import (
    ConsolidationPolicyRepository,
    ConsolidationRunRepository,
    MemoryIndexRepository,
    ProjectRepository,
)

NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)


class FlowMem0:
    def __init__(self) -> None:
        self.memories = {
            "canonical": {
                "id": "canonical",
                "memory": "same",
                "metadata": {
                    "_mem0_sidecar_project_id": "repo-a",
                    "_mem0_sidecar_app_id": "app-a",
                },
            },
            "redundant": {
                "id": "redundant",
                "memory": "same",
                "metadata": {
                    "_mem0_sidecar_project_id": "repo-a",
                    "_mem0_sidecar_app_id": "app-a",
                },
            },
        }

    async def get_memory(self, memory_id: str):
        if memory_id not in self.memories:
            raise Mem0UpstreamError(
                method="GET",
                path=f"/memories/{memory_id}",
                status_code=404,
                response_text="not found",
                message="not found",
            )
        return self.memories[memory_id]

    async def search_memories(self, _payload):
        return {"results": list(self.memories.values())}

    async def delete_memory(self, memory_id: str):
        self.memories.pop(memory_id, None)
        return {"id": memory_id, "deleted": True}


@pytest.mark.asyncio
async def test_exact_duplicate_checkpoint_shadow_search_and_finalize(db_session):
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
    content_hash, content_length = memory_content_fingerprint({"memory": "same"})
    assert content_hash is not None
    memories = MemoryIndexRepository(db_session)
    for memory_id in ("canonical", "redundant"):
        memories.upsert_memory(
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
            observed_at=NOW,
        )
    run = ConsolidationRunRepository(db_session).create(policy, now=NOW)
    db_session.commit()
    mem0 = FlowMem0()
    consolidation = ConsolidationService(
        session=db_session,
        mem0=mem0,
        bridge_routing_ready=True,
        hard_delete_enabled=True,
        now=lambda: NOW,
    )

    await consolidation.run_scan(run.id)
    proposal = db_session.query(ConsolidationProposal).one()
    await consolidation.approve_proposal(
        proposal.id,
        expected_status="PENDING",
        expected_source_hashes={
            "canonical": content_hash,
            "redundant": content_hash,
        },
    )
    shadowed = await consolidation.shadow_approved(proposal.id)
    memory_service = MemoryService(session=db_session, mem0=mem0)
    shadowed_detail = await memory_service.get_memory(
        project_id="repo-a",
        memory_id="redundant",
        request_app_id="app-a",
    )
    search = await memory_service.search_memories(
        project_id="repo-a",
        payload={"query": "same", "user_id": "root", "app_id": "app-a"},
    )
    applied = await consolidation.finalize_shadowed(
        proposal.id,
        now=NOW + timedelta(days=8),
    )

    assert shadowed["status"] == "SHADOWED"
    assert shadowed_detail["id"] == "redundant"
    assert shadowed_detail["memory"] == "same"
    assert [item["id"] for item in search["results"]] == ["canonical"]
    assert applied["status"] == "APPLIED"
    assert list(mem0.memories) == ["canonical"]
    assert db_session.query(ConsolidationLineage).one().source_memory_id == (
        "redundant"
    )
