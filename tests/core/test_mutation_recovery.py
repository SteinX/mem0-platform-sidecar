import asyncio
import json
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from mem0_sidecar.core.entities import EntityService
from mem0_sidecar.core.memory_ops import MemoryService, MutationConflictError
from mem0_sidecar.mem0_client.client import Mem0RestClient, Mem0UpstreamError
from mem0_sidecar.store.database import create_engine_from_url, create_session_factory
from mem0_sidecar.store.models import Base, Entity, Event, MemoryIndex, MutationIntent
from mem0_sidecar.store.repositories import (
    EntityRepository,
    MemoryIndexRepository,
    MutationIntentRepository,
    ProjectRepository,
)

PROJECT_ID = "recovery-project"
APP_ID = "recovery-app"
MUTATION_MARKER = "_mem0_sidecar_mutation_id"


class _StatefulRecoveryClient:
    def __init__(self, *, cancel_operation: str | None = None) -> None:
        self.cancel_operation = cancel_operation
        self.cancelled = False
        self.records: dict[str, dict[str, Any]] = {}
        self.add_calls = 0
        self.deleted_ids: list[str] = []
        self.get_ids: list[str] = []
        self.update_ids: list[str] = []
        self.list_calls = 0

    def _cancel_once(self, operation: str) -> None:
        if self.cancel_operation == operation and not self.cancelled:
            self.cancelled = True
            raise __import__("asyncio").CancelledError()

    async def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.add_calls += 1
        memory_id = f"added-{self.add_calls}"
        self.records[memory_id] = {
            "id": memory_id,
            "memory": payload["text"],
            "app_id": APP_ID,
            "metadata": dict(payload.get("metadata") or {}),
        }
        self._cancel_once("add")
        return dict(self.records[memory_id])

    async def update_memory(
        self,
        memory_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.update_ids.append(memory_id)
        record = self.records[memory_id]
        if "text" in payload:
            record["memory"] = payload["text"]
        if "metadata" in payload:
            record["metadata"] = dict(payload["metadata"] or {})
        self._cancel_once("update")
        return {"id": memory_id, "updated": True}

    async def get_memory(self, memory_id: str) -> dict[str, Any]:
        self.get_ids.append(memory_id)
        try:
            return dict(self.records[memory_id])
        except KeyError as exc:
            raise Mem0UpstreamError(
                method="GET",
                path=f"/memories/{memory_id}",
                status_code=404,
                response_text="not found",
                message="not found",
            ) from exc

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        existed = self.records.pop(memory_id, None) is not None
        self.deleted_ids.append(memory_id)
        self._cancel_once("entity" if memory_id == "entity-one" else "delete")
        if not existed:
            raise Mem0UpstreamError(
                method="DELETE",
                path=f"/memories/{memory_id}",
                status_code=404,
                response_text="not found",
                message="not found",
            )
        return {"id": memory_id, "deleted": True}

    async def list_memories(self, params: dict[str, Any]) -> dict[str, Any]:
        self.list_calls += 1
        return {"results": list(self.records.values()), "total": len(self.records)}


class _RealHttpOutcomeClient:
    """Stateful upstream exercised through the production REST client."""

    def __init__(self, *, operation: str, failure_kind: str) -> None:
        self.operation = operation
        self.failure_kind = failure_kind
        self.failure_injected = False
        self.records: dict[str, dict[str, Any]] = {}
        self.write_calls: list[tuple[str, str]] = []
        self.read_calls: list[tuple[str, str]] = []
        self.client = Mem0RestClient(
            base_url="http://mem0.local",
            transport=httpx.MockTransport(self._handler),
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)

    def _lost_response(self, request: httpx.Request) -> httpx.Response:
        self.failure_injected = True
        if self.failure_kind == "read-timeout":
            raise httpx.ReadTimeout("response timed out", request=request)
        if self.failure_kind == "disconnect":
            raise httpx.RemoteProtocolError(
                "peer disconnected before response",
                request=request,
            )
        assert self.failure_kind == "invalid-json"
        return httpx.Response(
            200,
            content=b"not-json sk-response-body-secret",
            headers={"content-type": "application/json"},
        )

    def _should_lose_response(self, operation: str) -> bool:
        if operation != self.operation or self.failure_injected:
            return False
        if operation == "entity":
            return len(self.write_calls) == 2
        return True

    async def _handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/memories":
            payload = json.loads(request.content)
            memory_id = "added-http"
            self.records[memory_id] = {
                "id": memory_id,
                "memory": payload["text"],
                "user_id": payload.get("user_id"),
                "app_id": payload.get("app_id"),
                "metadata": dict(payload.get("metadata") or {}),
            }
            self.write_calls.append(("POST", path))
            if self._should_lose_response("add"):
                return self._lost_response(request)
            return httpx.Response(200, json=dict(self.records[memory_id]))

        if request.method == "GET" and path == "/memories":
            self.read_calls.append(("GET", path))
            return httpx.Response(
                200,
                json={
                    "results": list(self.records.values()),
                    "total": len(self.records),
                },
            )

        assert path.startswith("/memories/")
        memory_id = path.removeprefix("/memories/")
        if request.method == "GET":
            self.read_calls.append(("GET", path))
            record = self.records.get(memory_id)
            if record is None:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(200, json=dict(record))

        if request.method == "PUT":
            payload = json.loads(request.content)
            record = self.records[memory_id]
            if "text" in payload:
                record["memory"] = payload["text"]
            if "metadata" in payload:
                record["metadata"] = dict(payload["metadata"] or {})
            self.write_calls.append(("PUT", path))
            if self._should_lose_response("update"):
                return self._lost_response(request)
            return httpx.Response(200, json={"id": memory_id, "updated": True})

        assert request.method == "DELETE"
        self.records.pop(memory_id, None)
        self.write_calls.append(("DELETE", path))
        if self._should_lose_response(self.operation):
            return self._lost_response(request)
        return httpx.Response(200, json={"id": memory_id, "deleted": True})


def _session_factory(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'recovery.sqlite3'}")
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


def _seed_memory(factory, client, memory_id: str, *, user_id: str = "alice") -> None:
    client.records[memory_id] = {
        "id": memory_id,
        "memory": "old",
        "user_id": user_id,
        "app_id": APP_ID,
        "metadata": {"version": "old"},
    }
    with factory() as session:
        MemoryIndexRepository(session).upsert_memory(
            project_id=PROJECT_ID,
            mem0_memory_id=memory_id,
            user_id=user_id,
            app_id=APP_ID,
            category=None,
            metadata={"version": "old"},
        )
        EntityRepository(session).rebuild_project_entities(PROJECT_ID, APP_ID)
        session.commit()


def _intent_state(factory) -> dict[str, Any]:
    with factory() as session:
        intent = session.scalar(select(MutationIntent))
        assert intent is not None
        return {
            "status": intent.status,
            "attempt_count": intent.attempt_count,
            "lease_expires_at": intent.lease_expires_at,
            "payload": json.loads(intent.payload_json),
        }


async def _invoke_mutation(factory, client, operation: str) -> None:
    with factory() as session:
        if operation == "add":
            await MemoryService(session=session, mem0=client).add_memory(
                project_id=PROJECT_ID,
                payload={"text": "hello", "app_id": APP_ID},
            )
        elif operation == "update":
            await MemoryService(session=session, mem0=client).update_memory(
                project_id=PROJECT_ID,
                memory_id="memory-one",
                request_app_id=APP_ID,
                payload={"metadata": {"version": "new"}},
            )
        elif operation == "delete":
            await MemoryService(session=session, mem0=client).delete_memory(
                project_id=PROJECT_ID,
                memory_id="memory-one",
                request_app_id=APP_ID,
            )
        else:
            await EntityService(session=session, mem0=client).delete_entity(
                PROJECT_ID,
                APP_ID,
                "user",
                "alice",
            )


async def _recover(factory, client) -> dict[str, int]:
    with factory() as session:
        service = MemoryService(session=session, mem0=client)
        recovery = getattr(service, "recover_pending_mutations", None)
        assert callable(recovery), "MemoryService must expose production recovery"
        return await recovery(project_id=PROJECT_ID, app_id=APP_ID)


def _assert_recovered(factory, client, operation: str) -> None:
    with factory() as session:
        intents = session.execute(
            text(
                "SELECT operation, status FROM mutation_intents "
                "WHERE project_id = :project_id AND app_id = :app_id"
            ),
            {"project_id": PROJECT_ID, "app_id": APP_ID},
        ).mappings().all()
        assert intents and all(row["status"] == "COMPLETED" for row in intents)
        assert all(
            event.status.value == "SUCCEEDED"
            for event in session.scalars(select(Event))
        )
        if operation == "add":
            active = list(
                session.scalars(
                    select(MemoryIndex).where(MemoryIndex.deleted_at.is_(None))
                )
            )
            assert len(active) == 1
            assert active[0].mem0_memory_id in client.records
        elif operation == "update":
            memory = session.scalar(
                select(MemoryIndex).where(MemoryIndex.mem0_memory_id == "memory-one")
            )
            assert memory is not None
            assert json.loads(memory.metadata_projection_json)["version"] == "new"
        elif operation == "delete":
            memory = session.scalar(
                select(MemoryIndex).where(MemoryIndex.mem0_memory_id == "memory-one")
            )
            assert memory is not None and memory.deleted_at is not None
        else:
            memories = list(session.scalars(select(MemoryIndex)))
            assert len(memories) == 2
            assert all(memory.deleted_at is not None for memory in memories)
            assert list(session.scalars(select(Entity))) == []


@pytest.mark.asyncio
async def test_lock_failure_before_delete_is_terminal_and_never_replayed(
    tmp_path,
    monkeypatch,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient()
    _seed_memory(factory, client, "memory-one")
    real_lock = ProjectRepository.lock_for_mutation
    lock_attempts = 0

    def fail_lock(self, project_id: str):
        nonlocal lock_attempts
        lock_attempts += 1
        if lock_attempts == 1:
            return real_lock(self, project_id)
        raise RuntimeError("injected lock timeout before upstream call")

    monkeypatch.setattr(ProjectRepository, "lock_for_mutation", fail_lock)
    with pytest.raises(RuntimeError, match="lock timeout"):
        await _invoke_mutation(factory, client, "delete")

    assert _intent_state(factory)["status"] == "FAILED"
    assert client.deleted_ids == []
    monkeypatch.setattr(ProjectRepository, "lock_for_mutation", real_lock)
    with factory() as session:
        await MemoryService(session=session, mem0=client).add_memory(
            project_id=PROJECT_ID,
            payload={"text": "unrelated", "app_id": APP_ID},
        )
    assert client.deleted_ids == []
    assert "memory-one" in client.records


@pytest.mark.asyncio
async def test_known_upstream_500_is_terminal_and_never_replayed(
    tmp_path,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient()
    _seed_memory(factory, client, "memory-one")
    delete_attempts = 0

    async def fail_delete(memory_id: str) -> dict[str, Any]:
        nonlocal delete_attempts
        delete_attempts += 1
        raise Mem0UpstreamError(
            method="DELETE",
            path=f"/memories/{memory_id}",
            status_code=500,
            response_text="not applied",
            message="not applied",
        )

    client.delete_memory = fail_delete
    with pytest.raises(Mem0UpstreamError):
        await _invoke_mutation(factory, client, "delete")

    assert _intent_state(factory)["status"] == "FAILED"
    with factory() as session:
        await MemoryService(session=session, mem0=client).add_memory(
            project_id=PROJECT_ID,
            payload={"text": "unrelated", "app_id": APP_ID},
        )
    assert delete_attempts == 1
    assert "memory-one" in client.records


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_kind",
    ["read-timeout", "disconnect", "invalid-json"],
)
@pytest.mark.parametrize("operation", ["add", "update", "delete", "entity"])
async def test_real_http_lost_response_is_unknown_then_converges_by_reads_only(
    tmp_path,
    operation: str,
    failure_kind: str,
) -> None:
    factory = _session_factory(tmp_path)
    client = _RealHttpOutcomeClient(
        operation=operation,
        failure_kind=failure_kind,
    )
    if operation in {"update", "delete"}:
        _seed_memory(factory, client, "memory-one")
    elif operation == "entity":
        _seed_memory(factory, client, "entity-one")
        _seed_memory(factory, client, "entity-two")

    with pytest.raises(Mem0UpstreamError) as exc_info:
        await _invoke_mutation(factory, client, operation)

    assert exc_info.value.outcome_unknown is True
    assert _intent_state(factory)["status"] == "UNKNOWN"
    writes_after_lost_response = list(client.write_calls)
    if operation == "entity":
        with factory() as session:
            local_deleted_at = list(
                session.scalars(
                    select(MemoryIndex.deleted_at).order_by(
                        MemoryIndex.mem0_memory_id
                    )
                )
            )
        assert local_deleted_at == [None, None]

    result = await _recover(factory, client)

    assert result == {"recovered": 1, "failed": 0}
    assert _intent_state(factory)["status"] == "COMPLETED"
    assert client.write_calls == writes_after_lost_response
    assert len(client.write_calls) == (2 if operation == "entity" else 1)
    assert client.read_calls
    with factory() as session:
        active = list(
            session.scalars(
                select(MemoryIndex).where(MemoryIndex.deleted_at.is_(None))
            )
        )
    if operation in {"delete", "entity"}:
        assert active == []
    else:
        assert len(active) == 1


@pytest.mark.asyncio
async def test_cancelled_add_after_apply_is_unknown_then_observed_complete(
    tmp_path,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient(cancel_operation="add")

    with pytest.raises(asyncio.CancelledError):
        await _invoke_mutation(factory, client, "add")
    assert _intent_state(factory)["status"] == "UNKNOWN"

    result = await _recover(factory, client)
    assert result["recovered"] == 1
    assert _intent_state(factory)["status"] == "COMPLETED"
    assert client.add_calls == 1
    assert client.deleted_ids == []


@pytest.mark.asyncio
async def test_cancelled_update_after_apply_completes_only_on_effect_match(
    tmp_path,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient(cancel_operation="update")
    _seed_memory(factory, client, "memory-one")

    with pytest.raises(asyncio.CancelledError):
        await _invoke_mutation(factory, client, "update")
    state = _intent_state(factory)
    assert state["status"] == "UNKNOWN"
    assert "expected_effect" in state["payload"]
    assert "version" not in json.dumps(state["payload"]["expected_effect"])

    result = await _recover(factory, client)
    assert result["recovered"] == 1
    assert _intent_state(factory)["status"] == "COMPLETED"
    assert client.update_ids == ["memory-one"]


@pytest.mark.asyncio
async def test_cancelled_update_before_apply_stays_unknown_on_effect_mismatch(
    tmp_path,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient()
    _seed_memory(factory, client, "memory-one")

    async def cancel_before_update(
        memory_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        client.update_ids.append(memory_id)
        raise asyncio.CancelledError()

    client.update_memory = cancel_before_update
    with pytest.raises(asyncio.CancelledError):
        await _invoke_mutation(factory, client, "update")

    with pytest.raises(MutationConflictError, match="unresolved"):
        await _recover(factory, client)
    assert _intent_state(factory)["status"] == "UNKNOWN"
    assert client.update_ids == ["memory-one"]
    assert client.records["memory-one"]["metadata"] == {"version": "old"}


@pytest.mark.asyncio
async def test_cancelled_delete_after_apply_uses_get_404_without_second_delete(
    tmp_path,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient(cancel_operation="delete")
    _seed_memory(factory, client, "memory-one")

    with pytest.raises(asyncio.CancelledError):
        await _invoke_mutation(factory, client, "delete")
    assert _intent_state(factory)["status"] == "UNKNOWN"

    result = await _recover(factory, client)
    assert result["recovered"] == 1
    assert _intent_state(factory)["status"] == "COMPLETED"
    assert client.deleted_ids == ["memory-one"]
    assert client.get_ids == ["memory-one"]


@pytest.mark.asyncio
async def test_cancelled_delete_before_apply_stays_unknown_when_target_exists(
    tmp_path,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient()
    _seed_memory(factory, client, "memory-one")

    async def cancel_before_delete(memory_id: str) -> dict[str, Any]:
        client.deleted_ids.append(memory_id)
        raise asyncio.CancelledError()

    client.delete_memory = cancel_before_delete
    with pytest.raises(asyncio.CancelledError):
        await _invoke_mutation(factory, client, "delete")

    with pytest.raises(MutationConflictError, match="unresolved"):
        await _recover(factory, client)
    assert _intent_state(factory)["status"] == "UNKNOWN"
    assert client.deleted_ids == ["memory-one"]
    assert client.get_ids == ["memory-one"]
    assert "memory-one" in client.records


@pytest.mark.asyncio
async def test_entity_recovery_never_deletes_targets_that_still_exist(tmp_path) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient()
    _seed_memory(factory, client, "entity-one")
    _seed_memory(factory, client, "entity-two")

    async def cancel_after_first_delete(memory_id: str) -> dict[str, Any]:
        client.deleted_ids.append(memory_id)
        client.records.pop(memory_id, None)
        raise asyncio.CancelledError()

    client.delete_memory = cancel_after_first_delete

    with pytest.raises(asyncio.CancelledError):
        await _invoke_mutation(factory, client, "entity")
    result = await _recover(factory, client)

    assert result == {"recovered": 0, "failed": 1}
    assert _intent_state(factory)["status"] == "PARTIAL"
    assert len(client.deleted_ids) == 1
    deleted_id = client.deleted_ids[0]
    remaining_id = ({"entity-one", "entity-two"} - {deleted_id}).pop()
    assert set(client.get_ids) == {"entity-one", "entity-two"}
    assert set(client.records) == {remaining_id}
    with factory() as session:
        memories = {
            item.mem0_memory_id: item.deleted_at
            for item in session.scalars(select(MemoryIndex))
        }
    assert memories[deleted_id] is not None
    assert memories[remaining_id] is None


@pytest.mark.asyncio
async def test_entity_recovery_all_targets_present_stays_unknown(tmp_path) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient()
    _seed_memory(factory, client, "entity-one")
    _seed_memory(factory, client, "entity-two")

    async def cancel_before_delete(memory_id: str) -> dict[str, Any]:
        client.deleted_ids.append(memory_id)
        raise asyncio.CancelledError()

    client.delete_memory = cancel_before_delete
    with pytest.raises(asyncio.CancelledError):
        await _invoke_mutation(factory, client, "entity")
    with pytest.raises(MutationConflictError, match="unresolved"):
        await _recover(factory, client)

    assert _intent_state(factory)["status"] == "UNKNOWN"
    assert len(client.deleted_ids) == 1
    assert set(client.get_ids) == {"entity-one", "entity-two"}
    assert set(client.records) == {"entity-one", "entity-two"}


@pytest.mark.asyncio
async def test_entity_recovery_all_targets_missing_completes_without_delete(
    tmp_path,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient()
    _seed_memory(factory, client, "entity-one")
    _seed_memory(factory, client, "entity-two")
    delete_count = 0

    async def cancel_after_last_delete(memory_id: str) -> dict[str, Any]:
        nonlocal delete_count
        delete_count += 1
        client.deleted_ids.append(memory_id)
        client.records.pop(memory_id, None)
        if delete_count == 2:
            raise asyncio.CancelledError()
        return {"id": memory_id, "deleted": True}

    client.delete_memory = cancel_after_last_delete
    with pytest.raises(asyncio.CancelledError):
        await _invoke_mutation(factory, client, "entity")
    result = await _recover(factory, client)

    assert result == {"recovered": 1, "failed": 0}
    assert _intent_state(factory)["status"] == "COMPLETED"
    assert set(client.deleted_ids) == {"entity-one", "entity-two"}
    assert set(client.get_ids) == {"entity-one", "entity-two"}


@pytest.mark.asyncio
async def test_recovery_claim_attempt_survives_observation_failure_and_restart(
    tmp_path,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient()

    async def cancel_before_add(payload: dict[str, Any]) -> dict[str, Any]:
        client.add_calls += 1
        raise asyncio.CancelledError()

    client.add_memory = cancel_before_add
    with pytest.raises(asyncio.CancelledError):
        await _invoke_mutation(factory, client, "add")

    async def fail_observation(params: dict[str, Any]) -> dict[str, Any]:
        client.list_calls += 1
        raise RuntimeError("observation unavailable")

    client.list_memories = fail_observation
    with pytest.raises(MutationConflictError, match="unresolved"):
        await _recover(factory, client)
    state = _intent_state(factory)
    assert state["status"] == "UNKNOWN"
    assert state["attempt_count"] == 2
    assert client.list_calls == 1


@pytest.mark.asyncio
async def test_recovery_exhaustion_persists_and_continues_blocking_scope(
    tmp_path,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient()

    async def cancel_before_add(payload: dict[str, Any]) -> dict[str, Any]:
        client.add_calls += 1
        raise asyncio.CancelledError()

    client.add_memory = cancel_before_add
    with pytest.raises(asyncio.CancelledError):
        await _invoke_mutation(factory, client, "add")

    with pytest.raises(MutationConflictError, match="unresolved"):
        await _recover(factory, client)
    assert _intent_state(factory)["attempt_count"] == 2
    with pytest.raises(MutationConflictError, match="exhausted"):
        await _recover(factory, client)
    exhausted = _intent_state(factory)
    assert exhausted["status"] == "EXHAUSTED"
    assert exhausted["attempt_count"] == 3
    observed_calls = client.list_calls

    with factory() as session:
        with pytest.raises(MutationConflictError, match="exhausted"):
            await MemoryService(session=session, mem0=client).add_memory(
                project_id=PROJECT_ID,
                payload={"text": "blocked", "app_id": APP_ID},
            )
    assert client.list_calls == observed_calls
    assert client.add_calls == 1


@pytest.mark.asyncio
async def test_legacy_pending_intent_is_normalized_without_replaying_add(
    tmp_path,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient()

    async def cancel_before_add(payload: dict[str, Any]) -> dict[str, Any]:
        client.add_calls += 1
        raise asyncio.CancelledError()

    client.add_memory = cancel_before_add
    with pytest.raises(asyncio.CancelledError):
        await _invoke_mutation(factory, client, "add")
    with factory() as session:
        intent = session.scalar(select(MutationIntent))
        assert intent is not None
        intent.status = "PENDING"
        intent.lease_expires_at = None
        session.commit()

    with pytest.raises(MutationConflictError, match="unresolved"):
        await _recover(factory, client)
    assert _intent_state(factory)["status"] == "UNKNOWN"
    assert client.add_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["add", "update", "delete", "entity"])
async def test_cancellation_after_upstream_side_effect_is_durably_recovered(
    tmp_path,
    operation: str,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient(cancel_operation=operation)
    if operation in {"update", "delete"}:
        _seed_memory(factory, client, "memory-one")
    elif operation == "entity":
        _seed_memory(factory, client, "entity-one")
        _seed_memory(factory, client, "entity-two")

    with pytest.raises(__import__("asyncio").CancelledError):
        await _invoke_mutation(factory, client, operation)

    result = await _recover(factory, client)
    assert result["recovered"] == 1
    _assert_recovered(factory, client, operation)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["add", "update", "delete", "entity"])
async def test_local_commit_failure_leaves_durable_intent_and_recovery_converges(
    tmp_path,
    monkeypatch,
    operation: str,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient()
    if operation in {"update", "delete"}:
        _seed_memory(factory, client, "memory-one")
    elif operation == "entity":
        _seed_memory(factory, client, "entity-one")
        _seed_memory(factory, client, "entity-two")

    with factory() as session:
        commit_calls = 0
        real_commit = session.commit

        def fail_projection_commit() -> None:
            nonlocal commit_calls
            commit_calls += 1
            if commit_calls == 2:
                raise RuntimeError("injected owning projection commit failure")
            real_commit()

        monkeypatch.setattr(session, "commit", fail_projection_commit)
        service = (
            EntityService(session=session, mem0=client)
            if operation == "entity"
            else MemoryService(session=session, mem0=client)
        )
        assert callable(
            getattr(
                MemoryService(session=session, mem0=client),
                "recover_pending_mutations",
                None,
            )
        ), "MemoryService must expose production recovery"
        with pytest.raises(RuntimeError, match="injected owning projection"):
            if operation == "add":
                await service.add_memory(
                    project_id=PROJECT_ID,
                    payload={"text": "hello", "app_id": APP_ID},
                )
            elif operation == "update":
                await service.update_memory(
                    project_id=PROJECT_ID,
                    memory_id="memory-one",
                    request_app_id=APP_ID,
                    payload={"metadata": {"version": "new"}},
                )
            elif operation == "delete":
                await service.delete_memory(
                    project_id=PROJECT_ID,
                    memory_id="memory-one",
                    request_app_id=APP_ID,
                )
            else:
                await service.delete_entity(
                    PROJECT_ID, APP_ID, "user", "alice"
                )

    result = await _recover(factory, client)
    assert result["recovered"] == 1
    _assert_recovered(factory, client, operation)


@pytest.mark.asyncio
async def test_add_recovery_preserves_every_id_from_one_upstream_result(
    tmp_path,
    monkeypatch,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient()

    async def add_multiple(payload: dict[str, Any]) -> dict[str, Any]:
        client.add_calls += 1
        records = []
        for memory_id in ("multi-one", "multi-two"):
            record = {
                "id": memory_id,
                "memory": payload["text"],
                "app_id": APP_ID,
                "metadata": dict(payload.get("metadata") or {}),
            }
            client.records[memory_id] = record
            records.append(dict(record))
        return {"results": records}

    client.add_memory = add_multiple
    with factory() as session:
        commit_calls = 0
        real_commit = session.commit

        def fail_projection_commit() -> None:
            nonlocal commit_calls
            commit_calls += 1
            if commit_calls == 2:
                raise RuntimeError("injected owning projection commit failure")
            real_commit()

        monkeypatch.setattr(session, "commit", fail_projection_commit)
        with pytest.raises(RuntimeError, match="injected owning projection"):
            await MemoryService(session=session, mem0=client).add_memory(
                project_id=PROJECT_ID,
                payload={"text": "hello", "app_id": APP_ID},
            )

    result = await _recover(factory, client)
    assert result["recovered"] == 1
    assert set(client.records) == {"multi-one", "multi-two"}
    assert client.deleted_ids == []
    with factory() as session:
        active_ids = set(
            session.scalars(
                select(MemoryIndex.mem0_memory_id).where(
                    MemoryIndex.deleted_at.is_(None)
                )
            )
        )
    assert active_ids == {"multi-one", "multi-two"}


@pytest.mark.asyncio
async def test_identical_adds_without_client_key_are_distinct_operations(
    tmp_path,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient()
    payload = {"text": "same content", "app_id": APP_ID}

    with factory() as session:
        await MemoryService(session=session, mem0=client).add_memory(
            project_id=PROJECT_ID,
            payload=payload,
        )
    with factory() as session:
        await MemoryService(session=session, mem0=client).add_memory(
            project_id=PROJECT_ID,
            payload=payload,
        )

    assert set(client.records) == {"added-1", "added-2"}
    markers = {
        record["metadata"][MUTATION_MARKER] for record in client.records.values()
    }
    assert len(markers) == 2


@pytest.mark.asyncio
async def test_lost_add_response_exact_key_retry_does_not_call_add_twice(
    tmp_path,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient(cancel_operation="add")
    payload = {"text": "same logical operation", "app_id": APP_ID}

    with pytest.raises(__import__("asyncio").CancelledError):
        with factory() as session:
            await MemoryService(session=session, mem0=client).add_memory(
                project_id=PROJECT_ID,
                payload=payload,
                idempotency_key="retry-key",
            )

    with factory() as session:
        result = await MemoryService(session=session, mem0=client).add_memory(
            project_id=PROJECT_ID,
            payload=payload,
            idempotency_key="retry-key",
        )

    assert result["memory"]["id"] == "added-1"
    assert client.add_calls == 1
    assert client.deleted_ids == []


@pytest.mark.asyncio
async def test_same_add_payload_with_different_keys_is_independent(
    tmp_path,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient()
    payload = {"text": "same content", "app_id": APP_ID}

    for key in ("operation-one", "operation-two"):
        with factory() as session:
            await MemoryService(session=session, mem0=client).add_memory(
                project_id=PROJECT_ID,
                payload=payload,
                idempotency_key=key,
            )

    assert set(client.records) == {"added-1", "added-2"}
    markers = {
        record["metadata"][MUTATION_MARKER] for record in client.records.values()
    }
    assert len(markers) == 2


@pytest.mark.asyncio
async def test_same_idempotency_key_with_different_payload_conflicts(
    tmp_path,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient()

    with factory() as session:
        await MemoryService(session=session, mem0=client).add_memory(
            project_id=PROJECT_ID,
            payload={"text": "first", "app_id": APP_ID},
            idempotency_key="reused-key",
        )
    with factory() as session:
        with pytest.raises(MutationConflictError, match="different request"):
            await MemoryService(session=session, mem0=client).add_memory(
                project_id=PROJECT_ID,
                payload={"text": "second", "app_id": APP_ID},
                idempotency_key="reused-key",
            )

    assert client.add_calls == 1


@pytest.mark.asyncio
async def test_same_client_key_is_isolated_by_project_and_app(tmp_path) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient()
    with factory() as session:
        ProjectRepository(session).upsert_default_project(
            project_id="other-project",
            name="other-project",
            mem0_base_url="http://mem0.invalid",
            default_app_id="other-app",
        )
        session.commit()

    for project_id, app_id in (
        (PROJECT_ID, APP_ID),
        (PROJECT_ID, "other-app"),
        ("other-project", APP_ID),
    ):
        with factory() as session:
            await MemoryService(session=session, mem0=client).add_memory(
                project_id=project_id,
                payload={"text": "same", "app_id": app_id},
                idempotency_key="shared-client-key",
            )

    markers = {
        record["metadata"][MUTATION_MARKER] for record in client.records.values()
    }
    assert client.add_calls == 3
    assert len(markers) == 3


@pytest.mark.asyncio
async def test_concurrent_same_key_unique_race_reuses_completed_result(
    tmp_path,
    monkeypatch,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient()
    payload = {"text": "once", "app_id": APP_ID}
    with factory() as session:
        await MemoryService(session=session, mem0=client).add_memory(
            project_id=PROJECT_ID,
            payload=payload,
            idempotency_key="racing-key",
        )

    real_find = MutationIntentRepository.find_by_operation_key
    find_calls = 0

    def miss_once(self, **kwargs):
        nonlocal find_calls
        find_calls += 1
        if find_calls == 1:
            return None
        return real_find(self, **kwargs)

    monkeypatch.setattr(MutationIntentRepository, "find_by_operation_key", miss_once)
    with factory() as session:
        result = await MemoryService(session=session, mem0=client).add_memory(
            project_id=PROJECT_ID,
            payload=payload,
            idempotency_key="racing-key",
        )

    assert result["memory"]["id"] == "added-1"
    assert client.add_calls == 1


@pytest.mark.asyncio
async def test_concurrent_same_key_across_two_sessions_calls_upstream_once(
    tmp_path,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient()
    entered_upstream = asyncio.Event()
    release_upstream = asyncio.Event()

    async def blocked_add(payload: dict[str, Any]) -> dict[str, Any]:
        client.add_calls += 1
        entered_upstream.set()
        await release_upstream.wait()
        record = {
            "id": "concurrent-one",
            "memory": payload["text"],
            "app_id": APP_ID,
            "metadata": dict(payload.get("metadata") or {}),
        }
        client.records[record["id"]] = record
        return dict(record)

    client.add_memory = blocked_add

    async def first_request() -> dict[str, Any]:
        with factory() as session:
            return await MemoryService(session=session, mem0=client).add_memory(
                project_id=PROJECT_ID,
                payload={"text": "once", "app_id": APP_ID},
                idempotency_key="concurrent-key",
            )

    first_task = asyncio.create_task(first_request())
    await entered_upstream.wait()
    try:
        with factory() as session:
            with pytest.raises(MutationConflictError, match="in progress"):
                await MemoryService(session=session, mem0=client).add_memory(
                    project_id=PROJECT_ID,
                    payload={"text": "once", "app_id": APP_ID},
                    idempotency_key="concurrent-key",
                )
    finally:
        release_upstream.set()

    first = await first_task
    assert first["memory"]["id"] == "concurrent-one"
    assert client.add_calls == 1


@pytest.mark.asyncio
async def test_unkeyed_unknown_add_blocks_next_unrelated_mutation(tmp_path) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient()
    original_add = client.add_memory

    async def cancel_before_apply(payload: dict[str, Any]) -> dict[str, Any]:
        client.add_calls += 1
        if client.add_calls == 1:
            raise __import__("asyncio").CancelledError()
        client.add_calls -= 1
        return await original_add(payload)

    client.add_memory = cancel_before_apply
    with pytest.raises(__import__("asyncio").CancelledError):
        with factory() as session:
            await MemoryService(session=session, mem0=client).add_memory(
                project_id=PROJECT_ID,
                payload={"text": "unknown", "app_id": APP_ID},
            )

    with factory() as session:
        with pytest.raises(MutationConflictError, match="unresolved"):
            await MemoryService(session=session, mem0=client).add_memory(
                project_id=PROJECT_ID,
                payload={"text": "unrelated", "app_id": APP_ID},
            )

    assert client.add_calls == 1
    assert client.records == {}


@pytest.mark.asyncio
async def test_completed_key_retry_returns_same_safe_response_as_first_call(
    tmp_path,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient()

    async def add_with_sensitive_result(payload: dict[str, Any]) -> dict[str, Any]:
        client.add_calls += 1
        memory_id = "sensitive-result"
        record = {
            "id": memory_id,
            "memory": payload["text"],
            "metadata": {
                **dict(payload.get("metadata") or {}),
                "api_key": "sk-sensitive-upstream-result",
            },
        }
        client.records[memory_id] = record
        return dict(record)

    client.add_memory = add_with_sensitive_result
    responses = []
    for _ in range(2):
        with factory() as session:
            responses.append(
                await MemoryService(session=session, mem0=client).add_memory(
                    project_id=PROJECT_ID,
                    payload={"text": "safe", "app_id": APP_ID},
                    idempotency_key="safe-result-key",
                )
            )

    assert responses[1] == responses[0]
    assert "sk-sensitive-upstream-result" not in json.dumps(responses[0])
    assert client.add_calls == 1


@pytest.mark.asyncio
async def test_lossy_sanitized_add_stays_unknown_without_replay_or_secret(
    tmp_path,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient()
    seen_payloads: list[dict[str, Any]] = []
    original_add = client.add_memory

    async def lose_first_attempt(payload):
        seen_payloads.append(json.loads(json.dumps(payload)))
        if len(seen_payloads) == 1:
            raise __import__("asyncio").CancelledError()
        return await original_add(payload)

    client.add_memory = lose_first_attempt
    add_payload = {
        "text": "remember safely",
        "app_id": APP_ID,
        "metadata": {"api_key": "sk-sensitive-value"},
    }
    with pytest.raises(__import__("asyncio").CancelledError):
        with factory() as session:
            await MemoryService(session=session, mem0=client).add_memory(
                project_id=PROJECT_ID,
                payload=add_payload,
            )

    with pytest.raises(MutationConflictError, match="unresolved"):
        await _recover(factory, client)
    assert client.add_calls == 0
    with factory() as session:
        assert session.scalar(select(Event)).status.value == "FAILED"
        row = session.execute(
            text("SELECT status, payload_json FROM mutation_intents")
        ).mappings().one()
        assert row["status"] == "UNKNOWN"
        assert "sk-sensitive-value" not in row["payload_json"]

    with factory() as session:
        with pytest.raises(MutationConflictError, match="unresolved"):
            await MemoryService(session=session, mem0=client).add_memory(
                project_id=PROJECT_ID,
                payload=add_payload,
            )
    assert len(seen_payloads) == 1
    assert client.records == {}
