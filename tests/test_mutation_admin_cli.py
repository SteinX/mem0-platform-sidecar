import asyncio
import importlib
import io
import json
import threading
import time
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from mem0_sidecar.core.memory_ops import (
    SIDECAR_APP_ID_METADATA_KEY,
    SIDECAR_MUTATION_ID_METADATA_KEY,
    SIDECAR_PROJECT_ID_METADATA_KEY,
    MemoryService,
    MutationConflictError,
)
from mem0_sidecar.core.mutation_admin import MutationAdminService
from mem0_sidecar.mem0_client.client import Mem0RestClient
from mem0_sidecar.store.database import create_engine_from_url, create_session_factory
from mem0_sidecar.store.models import Base, Event, EventStatus, MutationIntent
from mem0_sidecar.store.repositories import (
    EventRepository,
    MutationIntentRepository,
    ProjectRepository,
)

PROJECT_ID = "admin-project"
APP_ID = "admin-app"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _AdminTestClient:
    def __init__(self, *, cancel_first_add_before_effect: bool = False) -> None:
        self.cancel_first_add_before_effect = cancel_first_add_before_effect
        self.cancelled = False
        self.records: dict[str, dict[str, Any]] = {}
        self.read_calls: list[tuple[str, str]] = []
        self.write_calls: list[tuple[str, str]] = []

    async def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.write_calls.append(("POST", "/memories"))
        if self.cancel_first_add_before_effect and not self.cancelled:
            self.cancelled = True
            raise asyncio.CancelledError()
        memory_id = f"added-{len(self.write_calls)}"
        record = {
            "id": memory_id,
            "memory": payload["text"],
            "app_id": APP_ID,
            "metadata": dict(payload.get("metadata") or {}),
        }
        self.records[memory_id] = record
        return dict(record)

    async def update_memory(
        self,
        memory_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.write_calls.append(("PUT", f"/memories/{memory_id}"))
        return {"id": memory_id, "updated": True}

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        self.write_calls.append(("DELETE", f"/memories/{memory_id}"))
        self.records.pop(memory_id, None)
        return {"id": memory_id, "deleted": True}

    async def list_memories(self, params: dict[str, Any]) -> dict[str, Any]:
        self.read_calls.append(("GET", "/memories"))
        return {"results": list(self.records.values()), "total": len(self.records)}


class _BlockingObservationClient(_AdminTestClient):
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self.entered = entered
        self.release = release

    async def list_memories(self, params: dict[str, Any]) -> dict[str, Any]:
        self.read_calls.append(("GET", "/memories"))
        self.entered.set()
        assert self.release.wait(5)
        return {"results": [], "total": 0}


def _session_factory(tmp_path):
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
        ProjectRepository(session).upsert_default_project(
            project_id="other-project",
            name="other-project",
            mem0_base_url="http://mem0.invalid",
            default_app_id="other-app",
        )
        session.commit()
    return factory


def _load_cli_main():
    try:
        module = importlib.import_module("mem0_sidecar.management_cli")
    except ModuleNotFoundError:
        pytest.fail(
            "container-authenticated mutation management CLI is missing",
            pytrace=False,
        )
    main = getattr(module, "main", None)
    assert callable(main), "mem0_sidecar.management_cli.main is missing"
    return main


def _run_cli(factory, client, *arguments: str):
    stdout = io.StringIO()
    stderr = io.StringIO()
    main = _load_cli_main()
    code = main(
        list(arguments),
        session_factory=factory,
        mem0_client=client,
        stdout=stdout,
        stderr=stderr,
    )
    output = stdout.getvalue()
    payload = json.loads(output) if output.strip() else None
    return code, payload, stderr.getvalue()


async def _run_cli_async(factory, client, *arguments: str):
    result: dict[str, Any] = {}

    def run() -> None:
        try:
            result["value"] = _run_cli(factory, client, *arguments)
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while thread.is_alive() and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    if thread.is_alive():
        raise TimeoutError("mutation admin CLI thread did not finish")
    if "error" in result:
        raise result["error"]
    return result["value"]


def _seed_blocker(
    factory,
    *,
    project_id: str = PROJECT_ID,
    app_id: str = APP_ID,
    status: str = "EXHAUSTED",
    attempt_count: int = 3,
    marker: str = "marker-one",
    operation_key: str = "operation-key-one",
    secret: str = "sk-payload-must-never-leak",
) -> str:
    with factory() as session:
        event = EventRepository(session).create_event(
            project_id=project_id,
            app_id=app_id,
            operation="memory.add",
            request={"app_id": app_id, "text": secret},
            subject_type="memory",
        )
        intent = MutationIntentRepository(session).create(
            project_id=project_id,
            app_id=app_id,
            event_id=event.id,
            operation="memory.add",
            operation_key=operation_key,
            payload={
                "mutation_id": marker,
                "request_fingerprint": "fingerprint",
                "upstream_payload": {
                    "text": secret,
                    "metadata": {"api_key": secret},
                },
            },
        )
        intent.status = status
        intent.attempt_count = attempt_count
        intent.lease_expires_at = (
            datetime.now(UTC) + timedelta(minutes=5)
            if status == "ACTIVE"
            else None
        )
        session.commit()
        return intent.id


def _resolve_args(
    intent_id: str,
    *,
    project_id: str = PROJECT_ID,
    app_id: str = APP_ID,
    expected_status: str = "EXHAUSTED",
    expected_attempt_count: int = 3,
    reason: str = "operator accepted an unknowable upstream outcome",
) -> list[str]:
    return [
        "mutation-intents",
        "resolve",
        "--project-id",
        project_id,
        "--app-id",
        app_id,
        "--intent-id",
        intent_id,
        "--confirm-intent-id",
        intent_id,
        "--expected-status",
        expected_status,
        "--expected-attempt-count",
        str(expected_attempt_count),
        "--reason",
        reason,
        "--accept-unknown-outcome",
    ]


async def _exhaust_cancelled_no_effect_add(factory, client) -> str:
    with pytest.raises(asyncio.CancelledError):
        with factory() as session:
            await MemoryService(session=session, mem0=client).add_memory(
                project_id=PROJECT_ID,
                payload={"text": "ambiguous", "app_id": APP_ID},
                idempotency_key="old-key",
            )

    for expected_message in ("unresolved", "exhausted"):
        with factory() as session:
            with pytest.raises(MutationConflictError, match=expected_message):
                await MemoryService(
                    session=session,
                    mem0=client,
                ).recover_pending_mutations(project_id=PROJECT_ID, app_id=APP_ID)

    with factory() as session:
        intent = session.scalar(select(MutationIntent))
        assert intent is not None
        assert intent.status == "EXHAUSTED"
        assert intent.attempt_count == 3
        assert client.records == {}
        return intent.id


def test_pyproject_exposes_container_management_entrypoint() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]

    assert project.get("scripts", {}).get("mem0-sidecar-admin") == (
        "mem0_sidecar.management_cli:entrypoint"
    )


@pytest.mark.asyncio
async def test_add_marker_observation_runs_without_database_transaction(
    tmp_path,
) -> None:
    factory = _session_factory(tmp_path)
    intent_id = _seed_blocker(factory)
    with factory() as session:

        class TransactionInspectingClient(_AdminTestClient):
            async def list_memories(self, params: dict[str, Any]) -> dict[str, Any]:
                assert not session.in_transaction()
                return await super().list_memories(params)

        result = await MutationAdminService(
            session=session,
            mem0=TransactionInspectingClient(),
        ).resolve_intent(
            project_id=PROJECT_ID,
            app_id=APP_ID,
            intent_id=intent_id,
            confirmation_intent_id=intent_id,
            expected_status="EXHAUSTED",
            expected_attempt_count=3,
            reason="operator accepted the unknown outcome",
            accept_unknown_outcome=True,
        )

    assert result["intent"]["status"] == "FAILED"


@pytest.mark.asyncio
async def test_cancelled_no_effect_add_can_be_listed_resolved_and_unblocked(
    tmp_path,
) -> None:
    factory = _session_factory(tmp_path)
    client = _AdminTestClient(cancel_first_add_before_effect=True)
    intent_id = await _exhaust_cancelled_no_effect_add(factory, client)

    code, listing, error = await _run_cli_async(
        factory,
        client,
        "mutation-intents",
        "list",
        "--project-id",
        PROJECT_ID,
        "--app-id",
        APP_ID,
    )
    assert code == 0, error
    assert listing == {
        "count": 1,
        "intents": [
            {
                "id": intent_id,
                "operation": "memory.add",
                "status": "EXHAUSTED",
                "attempt_count": 3,
                "lease_expires_at": None,
            }
        ],
    }
    serialized_listing = json.dumps(listing)
    assert "ambiguous" not in serialized_listing
    assert "old-key" not in serialized_listing
    assert "payload" not in serialized_listing

    writes_before_resolution = list(client.write_calls)
    code, resolved, error = await _run_cli_async(
        factory,
        client,
        *_resolve_args(intent_id),
    )
    assert code == 0, error
    assert resolved["intent"] == {
        "id": intent_id,
        "operation": "memory.add",
        "status": "FAILED",
        "attempt_count": 3,
        "lease_expires_at": None,
    }
    assert resolved["audit_event_id"]
    assert client.write_calls == writes_before_resolution
    assert client.read_calls

    with factory() as session:
        intent = session.get(MutationIntent, intent_id)
        assert intent is not None
        assert intent.status == "FAILED"
        assert intent.lease_expires_at is None
        assert intent.completed_at is not None
        original_event = session.get(Event, intent.event_id)
        assert original_event is not None
        assert original_event.status == EventStatus.FAILED
        audits = list(
            session.scalars(
                select(Event).where(Event.operation == "mutation.resolve")
            )
        )
        assert len(audits) == 1
        assert audits[0].status == EventStatus.SUCCEEDED
        assert audits[0].subject_type == "mutation_intent"
        assert audits[0].subject_id == intent_id

    writes_before_old_key = list(client.write_calls)
    with factory() as session:
        with pytest.raises(MutationConflictError, match="outcome remains unresolved"):
            await MemoryService(session=session, mem0=client).add_memory(
                project_id=PROJECT_ID,
                payload={"text": "ambiguous", "app_id": APP_ID},
                idempotency_key="old-key",
            )
    assert client.write_calls == writes_before_old_key

    with factory() as session:
        result = await MemoryService(session=session, mem0=client).add_memory(
            project_id=PROJECT_ID,
            payload={"text": "new request", "app_id": APP_ID},
            idempotency_key="new-key",
        )
    assert result["memory"]["memory"] == "new request"
    assert len(client.write_calls) == len(writes_before_old_key) + 1


@pytest.mark.parametrize(
    ("status", "attempt_count"),
    [("PENDING", 1), ("ACTIVE", 1), ("FAILED", 3), ("COMPLETED", 3)],
)
def test_resolution_rejects_non_resolvable_intent_states(
    tmp_path,
    status: str,
    attempt_count: int,
) -> None:
    factory = _session_factory(tmp_path)
    client = _AdminTestClient()
    intent_id = _seed_blocker(
        factory,
        status=status,
        attempt_count=attempt_count,
    )

    code, _payload, error = _run_cli(
        factory,
        client,
        *_resolve_args(
            intent_id,
            expected_status=status,
            expected_attempt_count=attempt_count,
        ),
    )

    assert code != 0
    assert "cannot be resolved" in error
    with factory() as session:
        intent = session.get(MutationIntent, intent_id)
        assert intent is not None and intent.status == status
        assert list(
            session.scalars(select(Event).where(Event.operation == "mutation.resolve"))
        ) == []
    assert client.write_calls == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["--confirm-intent-id", "wrong-intent"],
        ["--expected-status", "UNKNOWN"],
        ["--expected-attempt-count", "2"],
        ["--project-id", "other-project"],
        ["--app-id", "other-app"],
    ],
)
def test_resolution_rejects_wrong_scope_confirmation_or_stale_cas(
    tmp_path,
    arguments: list[str],
) -> None:
    factory = _session_factory(tmp_path)
    client = _AdminTestClient()
    intent_id = _seed_blocker(factory)
    args = _resolve_args(intent_id)
    option, replacement = arguments
    args[args.index(option) + 1] = replacement

    code, _payload, error = _run_cli(factory, client, *args)

    assert code != 0
    assert error
    with factory() as session:
        intent = session.get(MutationIntent, intent_id)
        assert intent is not None and intent.status == "EXHAUSTED"
        assert intent.attempt_count == 3
    assert client.read_calls == []
    assert client.write_calls == []


@pytest.mark.parametrize(
    "omitted_option",
    ["--confirm-intent-id", "--accept-unknown-outcome"],
)
def test_resolution_requires_repeated_confirmation_and_unknown_acknowledgement(
    tmp_path,
    omitted_option: str,
) -> None:
    factory = _session_factory(tmp_path)
    client = _AdminTestClient()
    intent_id = _seed_blocker(factory)
    args = _resolve_args(intent_id)
    index = args.index(omitted_option)
    del args[index : index + (1 if omitted_option.startswith("--accept") else 2)]

    code, _payload, error = _run_cli(factory, client, *args)

    assert code == 2
    assert omitted_option in error
    with factory() as session:
        assert session.get(MutationIntent, intent_id).status == "EXHAUSTED"
    assert client.read_calls == []
    assert client.write_calls == []


def test_resolution_refuses_add_when_marker_is_observed(tmp_path) -> None:
    factory = _session_factory(tmp_path)
    client = _AdminTestClient()
    marker = "observed-marker"
    intent_id = _seed_blocker(factory, marker=marker)
    client.records["upstream-memory"] = {
        "id": "upstream-memory",
        "memory": "already applied",
        "app_id": APP_ID,
        "metadata": {
            SIDECAR_PROJECT_ID_METADATA_KEY: PROJECT_ID,
            SIDECAR_APP_ID_METADATA_KEY: APP_ID,
            SIDECAR_MUTATION_ID_METADATA_KEY: marker,
        },
    }

    code, _payload, error = _run_cli(
        factory,
        client,
        *_resolve_args(intent_id),
    )

    assert code != 0
    assert "marker was observed" in error
    with factory() as session:
        assert session.get(MutationIntent, intent_id).status == "EXHAUSTED"
    assert client.read_calls == [("GET", "/memories")]
    assert client.write_calls == []


def test_resolution_uses_default_compatible_bounded_marker_scan(tmp_path) -> None:
    factory = _session_factory(tmp_path)
    intent_id = _seed_blocker(factory)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        top_k = int(request.url.params["top_k"])
        if top_k > 1000:
            return httpx.Response(
                422,
                json={"detail": "top_k must be less than or equal to 1000"},
            )
        return httpx.Response(200, json={"results": [], "total": 0})

    client = Mem0RestClient(
        base_url="http://mem0.invalid",
        transport=httpx.MockTransport(handler),
    )

    code, resolved, error = _run_cli(
        factory,
        client,
        *_resolve_args(intent_id),
    )
    with factory() as session:
        status = session.get(MutationIntent, intent_id).status
    top_k_values = [request.url.params["top_k"] for request in requests]

    assert code == 0 and top_k_values == ["1000"] and status == "FAILED", (
        f"code={code} error={error!r} top_k={top_k_values!r} status={status}"
    )
    assert resolved["intent"]["status"] == "FAILED"


@pytest.mark.parametrize(
    "reason",
    ["", "   ", "x" * 513, "contains\x00control"],
)
def test_resolution_rejects_empty_oversized_or_control_reason(
    tmp_path,
    reason: str,
) -> None:
    factory = _session_factory(tmp_path)
    client = _AdminTestClient()
    intent_id = _seed_blocker(factory)

    code, _payload, error = _run_cli(
        factory,
        client,
        *_resolve_args(intent_id, reason=reason),
    )

    assert code != 0
    assert "reason" in error.lower()
    with factory() as session:
        assert session.get(MutationIntent, intent_id).status == "EXHAUSTED"
    assert client.read_calls == []
    assert client.write_calls == []


def test_resolution_audit_sanitizes_hostile_reason_and_exposes_no_payload(
    tmp_path,
) -> None:
    factory = _session_factory(tmp_path)
    client = _AdminTestClient()
    payload_secret = "sk-payload-secret"
    operation_key = "operation-key-must-stay-private"
    intent_id = _seed_blocker(
        factory,
        secret=payload_secret,
        operation_key=operation_key,
    )
    hostile_reason = (
        "token=sk-reason-secret "
        "http://mem0.invalid/private?api_key=reason-query-secret"
    )

    code, resolved, error = _run_cli(
        factory,
        client,
        *_resolve_args(intent_id, reason=hostile_reason),
    )

    assert code == 0, error
    serialized_output = json.dumps(resolved)
    for forbidden in (
        payload_secret,
        operation_key,
        "sk-reason-secret",
        "reason-query-secret",
        "upstream_payload",
    ):
        assert forbidden not in serialized_output
    with factory() as session:
        audit = session.scalar(
            select(Event).where(Event.operation == "mutation.resolve")
        )
        assert audit is not None
        serialized_audit = " ".join(
            (audit.request_json, audit.response_json, audit.error_json)
        )
        assert "[REDACTED]" in serialized_audit
        assert len(serialized_audit.encode()) <= 16_384
        for forbidden in (
            payload_secret,
            operation_key,
            "sk-reason-secret",
            "reason-query-secret",
            "upstream_payload",
        ):
            assert forbidden not in serialized_audit


def test_listing_is_bounded_payload_free_and_one_resolution_leaves_other_blockers(
    tmp_path,
) -> None:
    factory = _session_factory(tmp_path)
    client = _AdminTestClient()
    intent_ids = [
        _seed_blocker(
            factory,
            marker=f"marker-{index}",
            operation_key=f"operation-key-{index}",
            secret=f"sk-payload-{index}",
        )
        for index in range(105)
    ]

    code, listing, error = _run_cli(
        factory,
        client,
        "mutation-intents",
        "list",
        "--project-id",
        PROJECT_ID,
        "--app-id",
        APP_ID,
    )
    assert code == 0, error
    assert listing["count"] == 100
    assert len(listing["intents"]) == 100
    serialized = json.dumps(listing)
    assert len(serialized.encode()) <= 32_768
    assert "sk-payload" not in serialized
    assert "operation-key" not in serialized
    assert "marker-" not in serialized
    assert "payload" not in serialized

    first = listing["intents"][0]
    code, _resolved, error = _run_cli(
        factory,
        client,
        *_resolve_args(
            first["id"],
            expected_status=first["status"],
            expected_attempt_count=first["attempt_count"],
        ),
    )
    assert code == 0, error
    with factory() as session:
        blockers = MutationIntentRepository(session).list_blocking(
            PROJECT_ID,
            APP_ID,
        )
        assert blockers
        assert first["id"] not in {intent.id for intent in blockers}
        assert set(intent_ids) - {first["id"]}
        with pytest.raises(MutationConflictError, match="exhausted"):
            asyncio.run(
                MemoryService(
                    session=session,
                    mem0=client,
                ).recover_pending_mutations(project_id=PROJECT_ID, app_id=APP_ID)
            )


def test_sqlite_resolution_releases_project_lock_during_marker_observation(
    tmp_path,
) -> None:
    _load_cli_main()
    factory = _session_factory(tmp_path)
    intent_id = _seed_blocker(factory)
    entered = threading.Event()
    release = threading.Event()
    client = _BlockingObservationClient(entered, release)
    resolution_result: list[tuple[int, object, str]] = []
    recovery_result: list[object] = []
    failures: list[BaseException] = []

    def resolve() -> None:
        try:
            resolution_result.append(
                _run_cli(factory, client, *_resolve_args(intent_id))
            )
        except BaseException as exc:
            failures.append(exc)

    def recover() -> None:
        try:
            with factory() as session:
                recovery_result.append(
                    asyncio.run(
                        MemoryService(
                            session=session,
                            mem0=client,
                        ).recover_pending_mutations(
                            project_id=PROJECT_ID,
                            app_id=APP_ID,
                        )
                        )
                    )
        except MutationConflictError:
            recovery_result.append("scope-still-blocked")
        except BaseException as exc:
            failures.append(exc)

    resolution_thread = threading.Thread(target=resolve, daemon=True)
    recovery_thread = threading.Thread(target=recover, daemon=True)
    resolution_thread.start()
    assert entered.wait(3), "resolution did not reach read-only marker observation"
    recovery_thread.start()
    recovery_thread.join(0.35)
    progressed_during_observation = not recovery_thread.is_alive()
    release.set()
    resolution_thread.join(8)
    recovery_thread.join(8)

    assert not resolution_thread.is_alive()
    assert not recovery_thread.is_alive()
    assert failures == []
    assert progressed_during_observation
    assert resolution_result[0][0] == 0
    assert recovery_result == ["scope-still-blocked"]
    assert client.read_calls == [("GET", "/memories")]
    assert client.write_calls == []
    with factory() as session:
        assert session.get(MutationIntent, intent_id).status == "FAILED"
