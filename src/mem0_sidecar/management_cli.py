import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, TextIO

from mem0_sidecar.config import SidecarSettings, load_settings
from mem0_sidecar.core.consolidation_service import ConsolidationService
from mem0_sidecar.core.memory_ops import MemoryService
from mem0_sidecar.core.mutation_admin import MutationAdminError, MutationAdminService
from mem0_sidecar.mem0_client.client import Mem0RestClient
from mem0_sidecar.store.database import create_engine_from_url, create_session_factory
from mem0_sidecar.store.repositories import (
    ConsolidationPolicyRepository,
    ConsolidationProposalRepository,
    ConsolidationRunRepository,
    JobRepository,
    ServiceCapabilityRepository,
)


class _UsageError(ValueError):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="mem0-sidecar-admin")
    commands = parser.add_subparsers(dest="resource", required=True)
    intents = commands.add_parser("mutation-intents")
    actions = intents.add_subparsers(dest="action", required=True)

    list_parser = actions.add_parser("list")
    list_parser.add_argument("--project-id", required=True)
    list_parser.add_argument("--app-id", required=True)

    resolve_parser = actions.add_parser("resolve")
    resolve_parser.add_argument("--project-id", required=True)
    resolve_parser.add_argument("--app-id", required=True)
    resolve_parser.add_argument("--intent-id", required=True)
    resolve_parser.add_argument("--confirm-intent-id", required=True)
    resolve_parser.add_argument("--expected-status", required=True)
    resolve_parser.add_argument("--expected-attempt-count", required=True, type=int)
    resolve_parser.add_argument("--reason", required=True)
    resolve_parser.add_argument(
        "--accept-unknown-outcome",
        action="store_true",
        required=True,
    )

    direct_write_sync = commands.add_parser("direct-write-sync")
    direct_write_sync.add_argument("--once", action="store_true", required=True)
    direct_write_sync.add_argument("--project-id", required=True)
    direct_write_sync.add_argument("--default-app-id", required=True)
    direct_write_sync.add_argument(
        "--scan-limit",
        type=int,
        default=5000,
    )
    direct_write_sync.add_argument(
        "--legacy-cap",
        type=int,
        default=1000,
    )

    consolidation = commands.add_parser("consolidation")
    consolidation_resources = consolidation.add_subparsers(
        dest="consolidation_resource", required=True
    )
    policy = consolidation_resources.add_parser("policy")
    policy_actions = policy.add_subparsers(dest="consolidation_action", required=True)
    policy_get = policy_actions.add_parser("get")
    policy_get.add_argument("--project-id", required=True)
    policy_get.add_argument("--app-id", required=True)
    policy_set = policy_actions.add_parser("set")
    policy_set.add_argument("--project-id", required=True)
    policy_set.add_argument("--app-id", required=True)
    policy_set.add_argument("--confirm-app-id", required=True)
    policy_set.add_argument("--policy-json", required=True)

    run = consolidation_resources.add_parser("run")
    run.add_argument("--project-id", required=True)
    run.add_argument("--app-id", required=True)
    run.add_argument("--dry-run", action="store_true", required=True)

    proposals = consolidation_resources.add_parser("proposals")
    proposal_actions = proposals.add_subparsers(
        dest="consolidation_action", required=True
    )
    proposal_list = proposal_actions.add_parser("list")
    proposal_list.add_argument("--project-id", required=True)
    proposal_list.add_argument("--app-id", required=True)
    proposal_list.add_argument("--run-id", required=True)
    proposal_list.add_argument("--page", type=int, default=1)
    proposal_list.add_argument("--page-size", type=int, default=100)
    proposal_approve = proposal_actions.add_parser("approve")
    proposal_approve.add_argument("--project-id", required=True)
    proposal_approve.add_argument("--app-id", required=True)
    proposal_approve.add_argument("--proposal-id", required=True)
    proposal_approve.add_argument("--expected-status", required=True)
    proposal_approve.add_argument("--expected-source-hashes", required=True)
    proposal_approve.add_argument("--canonical-id")
    proposal_approve.add_argument("--replacement-text")
    proposal_reject = proposal_actions.add_parser("reject")
    proposal_reject.add_argument("--project-id", required=True)
    proposal_reject.add_argument("--app-id", required=True)
    proposal_reject.add_argument("--proposal-id", required=True)
    proposal_reject.add_argument("--expected-status", required=True)

    finalize = consolidation_resources.add_parser("finalize")
    finalize.add_argument("--project-id", required=True)
    finalize.add_argument("--app-id", required=True)
    finalize.add_argument("--proposal-id", required=True)
    finalize.add_argument(
        "--confirm-hard-delete", action="store_true", required=True
    )
    rollback = consolidation_resources.add_parser("rollback")
    rollback.add_argument("--project-id", required=True)
    rollback.add_argument("--app-id", required=True)
    rollback.add_argument("--proposal-id", required=True)
    return parser


def _mem0_client(settings: SidecarSettings) -> Mem0RestClient:
    return Mem0RestClient(
        base_url=settings.mem0_base_url,
        api_key=settings.mem0_api_key,
        api_key_header_name=settings.mem0_api_key_header_name,
        api_key_prefix=settings.mem0_api_key_prefix,
        extra_headers=settings.mem0_extra_headers,
        request_timeout_seconds=settings.mem0_request_timeout_seconds,
        connect_timeout_seconds=settings.mem0_connect_timeout_seconds,
        verify_tls=settings.mem0_verify_tls,
        ca_bundle=settings.mem0_ca_bundle,
        memories_path=settings.mem0_memories_path,
        search_path=settings.mem0_search_path,
    )


async def _execute(
    arguments: argparse.Namespace,
    *,
    session_factory,
    mem0_client: Any,
    settings: SidecarSettings,
) -> dict[str, Any]:
    with session_factory() as session:
        try:
            if arguments.resource == "direct-write-sync":
                return await MemoryService(
                    session=session,
                    mem0=mem0_client,
                ).mirror_direct_writes(
                    project_id=arguments.project_id,
                    default_app_id=arguments.default_app_id,
                    scan_limit=arguments.scan_limit,
                    legacy_cap=arguments.legacy_cap,
                )
            if arguments.resource == "consolidation":
                policies = ConsolidationPolicyRepository(session)
                proposals = ConsolidationProposalRepository(session)
                jobs = JobRepository(session)
                project_id = arguments.project_id
                app_id = arguments.app_id
                bridge_ready = (
                    not settings.consolidation_bridge_routing_required
                    or ServiceCapabilityRepository(
                        session
                    ).bridge_routing_ready(project_id)
                )
                service = ConsolidationService(
                    session=session,
                    mem0=mem0_client,
                    bridge_routing_ready=bridge_ready,
                    hard_delete_enabled=(
                        settings.consolidation_hard_delete_enabled
                    ),
                )
                if arguments.consolidation_resource == "policy":
                    if arguments.consolidation_action == "get":
                        row = policies.get(project_id, app_id)
                        if row is None:
                            raise ValueError("consolidation policy not found")
                        return policies.spec(row).to_mapping()
                    if arguments.confirm_app_id != app_id:
                        raise ValueError("app confirmation mismatch")
                    raw_policy = json.loads(arguments.policy_json)
                    if not isinstance(raw_policy, dict):
                        raise ValueError("policy must be an object")
                    row = policies.upsert(
                        project_id=project_id,
                        app_id=app_id,
                        policy=raw_policy,
                    )
                    result = policies.spec(row).to_mapping()
                    session.commit()
                    return result
                if arguments.consolidation_resource == "run":
                    policy = policies.get(project_id, app_id)
                    if policy is None:
                        raise ValueError("consolidation policy not found")
                    run = ConsolidationRunRepository(session).create(
                        policy, now=datetime.now(UTC)
                    )
                    run.mode = "OBSERVE"
                    job = jobs.enqueue(
                        project_id=project_id,
                        event_id=None,
                        job_type="consolidation.scan",
                        payload={"app_id": app_id, "run_id": run.id},
                        dedupe_key=f"manual:{run.id}",
                    )
                    session.commit()
                    result = await service.run_scan(run.id)
                    jobs.mark_succeeded(job.id, result)
                    session.commit()
                    return result
                if arguments.consolidation_resource == "proposals":
                    action = arguments.consolidation_action
                    if action == "list":
                        return service.list_proposals(
                            project_id=project_id,
                            app_id=app_id,
                            run_id=arguments.run_id,
                            page=arguments.page,
                            page_size=arguments.page_size,
                        )
                    proposal = proposals.get(arguments.proposal_id)
                    if proposal.project_id != project_id or proposal.app_id != app_id:
                        raise ValueError("proposal scope mismatch")
                    if action == "reject":
                        if proposal.status != arguments.expected_status:
                            raise ValueError("proposal status changed")
                        proposals.set_status(proposal, "REJECTED")
                        session.commit()
                        return {
                            "proposal_id": proposal.id,
                            "status": "REJECTED",
                        }
                    raw_hashes = json.loads(arguments.expected_source_hashes)
                    if not isinstance(raw_hashes, dict):
                        raise ValueError("expected hashes must be an object")
                    result = await service.approve_proposal(
                        proposal.id,
                        expected_status=arguments.expected_status,
                        expected_source_hashes=raw_hashes,
                        canonical_id=arguments.canonical_id,
                        replacement_text=arguments.replacement_text,
                    )
                    if result["status"] == "APPROVED":
                        jobs.enqueue(
                            project_id=project_id,
                            event_id=None,
                            job_type="consolidation.shadow",
                            payload={
                                "app_id": app_id,
                                "proposal_id": proposal.id,
                            },
                            dedupe_key=f"shadow:{proposal.id}",
                        )
                    session.commit()
                    return result
                proposal = proposals.get(arguments.proposal_id)
                if proposal.project_id != project_id or proposal.app_id != app_id:
                    raise ValueError("proposal scope mismatch")
                if arguments.consolidation_resource == "rollback":
                    return service.rollback_shadowed(proposal.id)
                return await service.finalize_shadowed(
                    proposal.id, now=datetime.now(UTC)
                )
            service = MutationAdminService(session=session, mem0=mem0_client)
            if arguments.action == "list":
                return service.list_blocking(
                    project_id=arguments.project_id,
                    app_id=arguments.app_id,
                )
            return await service.resolve_intent(
                project_id=arguments.project_id,
                app_id=arguments.app_id,
                intent_id=arguments.intent_id,
                confirmation_intent_id=arguments.confirm_intent_id,
                expected_status=arguments.expected_status,
                expected_attempt_count=arguments.expected_attempt_count,
                reason=arguments.reason,
                accept_unknown_outcome=arguments.accept_unknown_outcome,
            )
        except BaseException:
            session.rollback()
            raise


def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory=None,
    mem0_client: Any | None = None,
    settings: SidecarSettings | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    try:
        arguments = _parser().parse_args(list(argv) if argv is not None else None)
    except _UsageError as exc:
        stderr.write(f"error: {exc}\n")
        return 2

    settings = settings or load_settings()
    if session_factory is None or mem0_client is None:
        if session_factory is None:
            engine = create_engine_from_url(settings.database_url)
            session_factory = create_session_factory(engine)
        if mem0_client is None:
            mem0_client = _mem0_client(settings)

    try:
        result = asyncio.run(
            _execute(
                arguments,
                session_factory=session_factory,
                mem0_client=mem0_client,
                settings=settings,
            )
        )
    except (MutationAdminError, ValueError) as exc:
        if arguments.resource == "consolidation":
            stderr.write("error: consolidation operation failed\n")
            return 1
        stderr.write(f"error: {exc}\n")
        return 1
    except Exception:
        message = (
            "consolidation operation failed"
            if arguments.resource == "consolidation"
            else "mutation management operation failed"
        )
        stderr.write(f"error: {message}\n")
        return 1

    stdout.write(
        json.dumps(result, default=str, ensure_ascii=False, sort_keys=True) + "\n"
    )
    return 0


def entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
