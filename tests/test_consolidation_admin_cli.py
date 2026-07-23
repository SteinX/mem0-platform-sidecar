import io
import json
from datetime import UTC, datetime

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.management_cli import main
from mem0_sidecar.store.database import create_engine_from_url, create_session_factory
from mem0_sidecar.store.models import Base
from mem0_sidecar.store.repositories import (
    ConsolidationPolicyRepository,
    ConsolidationProposalRepository,
    ConsolidationRunRepository,
    MemoryIndexRepository,
    ProjectRepository,
)

PROJECT_ID = "repo-a"
APP_ID = "app-a"


class NoWriteMem0:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected upstream call: {name}")


def factory_for(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'admin.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        ProjectRepository(session).upsert_default_project(
            project_id=PROJECT_ID,
            name=PROJECT_ID,
            mem0_base_url="http://mem0.invalid",
            default_app_id=APP_ID,
        )
        session.commit()
    return factory


def run_cli(factory, *args: str, mem0_client=None):
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        list(args),
        session_factory=factory,
        mem0_client=mem0_client or NoWriteMem0(),
        settings=SidecarSettings(
            consolidation_bridge_routing_required=False,
        ),
        stdout=stdout,
        stderr=stderr,
    )
    payload = json.loads(stdout.getvalue()) if stdout.getvalue().strip() else None
    return code, payload, stderr.getvalue()


def seed_exact_proposal(factory) -> str:
    with factory() as session:
        policy = ConsolidationPolicyRepository(session).upsert(
            project_id=PROJECT_ID,
            app_id=APP_ID,
            policy={"enabled": True, "mode": "MANUAL"},
        )
        memories = MemoryIndexRepository(session)
        for memory_id in ("mem-a", "mem-b"):
            memories.upsert_memory(
                project_id=PROJECT_ID,
                mem0_memory_id=memory_id,
                user_id="root",
                app_id=APP_ID,
                category="decision",
                content_hash="same-hash",
                content_length=20,
                normalized_type="decision",
                source="manual",
                pinned=False,
                observed_at=datetime(2026, 7, 23, tzinfo=UTC),
            )
        run = ConsolidationRunRepository(session).create(
            policy, now=datetime(2026, 7, 23, tzinfo=UTC)
        )
        run.status = "SUCCEEDED"
        proposal = ConsolidationProposalRepository(session).create(
            run=run,
            proposal_key="exact-key",
            kind="EXACT_DUPLICATE",
            source_ids=("mem-a", "mem-b"),
            canonical_id="mem-a",
            score=None,
            evidence={"hash_prefix": "same-hash", "safe_action": True},
            status="PENDING",
        )
        session.commit()
        return proposal.id


def test_policy_set_requires_exact_app_confirmation_and_round_trips(tmp_path):
    factory = factory_for(tmp_path)
    policy_json = json.dumps({"enabled": True, "mode": "OBSERVE"})

    rejected = run_cli(
        factory,
        "consolidation",
        "policy",
        "set",
        "--project-id",
        PROJECT_ID,
        "--app-id",
        APP_ID,
        "--confirm-app-id",
        "wrong-app",
        "--policy-json",
        policy_json,
    )
    accepted = run_cli(
        factory,
        "consolidation",
        "policy",
        "set",
        "--project-id",
        PROJECT_ID,
        "--app-id",
        APP_ID,
        "--confirm-app-id",
        APP_ID,
        "--policy-json",
        policy_json,
    )
    fetched = run_cli(
        factory,
        "consolidation",
        "policy",
        "get",
        "--project-id",
        PROJECT_ID,
        "--app-id",
        APP_ID,
    )

    assert rejected[0] == 1 and rejected[1] is None
    assert rejected[2] == "error: consolidation operation failed\n"
    assert accepted[0] == 0 and accepted[1]["mode"] == "OBSERVE"
    assert fetched[0] == 0 and fetched[1] == accepted[1]


def test_approval_requires_optimistic_fields_and_rejects_exact_replacement_text(
    tmp_path,
) -> None:
    factory = factory_for(tmp_path)
    proposal_id = seed_exact_proposal(factory)
    hashes = json.dumps({"mem-a": "same-hash", "mem-b": "same-hash"})

    missing_status = run_cli(
        factory,
        "consolidation",
        "proposals",
        "approve",
        "--project-id",
        PROJECT_ID,
        "--app-id",
        APP_ID,
        "--proposal-id",
        proposal_id,
        "--expected-source-hashes",
        hashes,
    )
    secret = "replacement body must not leak"
    rejected = run_cli(
        factory,
        "consolidation",
        "proposals",
        "approve",
        "--project-id",
        PROJECT_ID,
        "--app-id",
        APP_ID,
        "--proposal-id",
        proposal_id,
        "--expected-status",
        "PENDING",
        "--expected-source-hashes",
        hashes,
        "--replacement-text",
        secret,
    )
    approved = run_cli(
        factory,
        "consolidation",
        "proposals",
        "approve",
        "--project-id",
        PROJECT_ID,
        "--app-id",
        APP_ID,
        "--proposal-id",
        proposal_id,
        "--expected-status",
        "PENDING",
        "--expected-source-hashes",
        hashes,
    )

    assert missing_status[0] == 2
    assert rejected[0] == 1
    assert secret not in (json.dumps(rejected[1]) + rejected[2])
    assert rejected[2] == "error: consolidation operation failed\n"
    assert approved[0] == 0
    assert approved[1] == {"proposal_id": proposal_id, "status": "APPROVED"}


def test_finalize_requires_explicit_hard_delete_confirmation(tmp_path) -> None:
    factory = factory_for(tmp_path)

    code, payload, stderr = run_cli(
        factory,
        "consolidation",
        "finalize",
        "--project-id",
        PROJECT_ID,
        "--app-id",
        APP_ID,
        "--proposal-id",
        "proposal-a",
    )

    assert code == 2
    assert payload is None
    assert "--confirm-hard-delete" in stderr


def test_scope_backfill_requires_confirmation_and_reports_remaining(tmp_path) -> None:
    factory = factory_for(tmp_path)
    with factory() as session:
        MemoryIndexRepository(session).upsert_memory(
            project_id=PROJECT_ID,
            mem0_memory_id="legacy",
            user_id="root",
            app_id=APP_ID,
            category="decision",
            metadata={"type": "decision"},
            content_hash="legacy-hash",
            content_length=12,
            normalized_type="decision",
            source="legacy",
            pinned=False,
            scope_markers_verified=False,
            observed_at=datetime(2026, 7, 23, tzinfo=UTC),
        )
        session.commit()

    class BackfillMem0:
        def __init__(self) -> None:
            self.record = {
                "id": "legacy",
                "memory": "legacy",
                "metadata": {"type": "decision"},
            }

        async def get_memory(self, _memory_id):
            return self.record

        async def update_memory(self, _memory_id, payload):
            self.record["metadata"] = payload["metadata"]
            return {"updated": True}

    mem0 = BackfillMem0()
    rejected = run_cli(
        factory,
        "consolidation",
        "scope-backfill",
        "--project-id",
        PROJECT_ID,
        "--app-id",
        APP_ID,
        "--confirm-app-id",
        "wrong-app",
        "--confirm-writes-paused",
        "--limit",
        "10",
        mem0_client=mem0,
    )
    accepted = run_cli(
        factory,
        "consolidation",
        "scope-backfill",
        "--project-id",
        PROJECT_ID,
        "--app-id",
        APP_ID,
        "--confirm-app-id",
        APP_ID,
        "--confirm-writes-paused",
        "--limit",
        "10",
        mem0_client=mem0,
    )

    assert rejected[0] == 1
    assert accepted[0] == 0
    assert accepted[1]["backfilled"] == 1
    assert accepted[1]["remaining"] == 0
