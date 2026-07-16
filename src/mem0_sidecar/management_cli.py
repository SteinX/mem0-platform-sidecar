import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any, TextIO

from mem0_sidecar.config import SidecarSettings, load_settings
from mem0_sidecar.core.mutation_admin import MutationAdminError, MutationAdminService
from mem0_sidecar.mem0_client.client import Mem0RestClient
from mem0_sidecar.store.database import create_engine_from_url, create_session_factory


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
) -> dict[str, Any]:
    with session_factory() as session:
        service = MutationAdminService(session=session, mem0=mem0_client)
        try:
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

    if session_factory is None or mem0_client is None:
        settings = load_settings()
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
            )
        )
    except (MutationAdminError, ValueError) as exc:
        stderr.write(f"error: {exc}\n")
        return 1
    except Exception:
        stderr.write("error: mutation management operation failed\n")
        return 1

    stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


def entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
