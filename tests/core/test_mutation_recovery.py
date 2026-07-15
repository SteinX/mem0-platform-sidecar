import json
from typing import Any

import pytest
from sqlalchemy import select, text

from mem0_sidecar.core.entities import EntityService
from mem0_sidecar.core.memory_ops import MemoryService
from mem0_sidecar.mem0_client.client import Mem0UpstreamError
from mem0_sidecar.store.database import create_engine_from_url, create_session_factory
from mem0_sidecar.store.models import Base, Entity, Event, MemoryIndex
from mem0_sidecar.store.repositories import (
    EntityRepository,
    MemoryIndexRepository,
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
        record = self.records[memory_id]
        if "text" in payload:
            record["memory"] = payload["text"]
        if "metadata" in payload:
            record["metadata"] = dict(payload["metadata"] or {})
        self._cancel_once("update")
        return {"id": memory_id, "updated": True}

    async def get_memory(self, memory_id: str) -> dict[str, Any]:
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
        return {"results": list(self.records.values()), "total": len(self.records)}


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
@pytest.mark.parametrize("mode", ["lost-response", "absent", "duplicates"])
async def test_add_recovery_replays_safely_and_canonicalizes_marker_duplicates(
    tmp_path,
    mode: str,
) -> None:
    factory = _session_factory(tmp_path)
    client = _StatefulRecoveryClient(cancel_operation="add")
    if mode == "absent":
        original_add = client.add_memory

        async def cancel_before_apply(payload):
            if not client.cancelled:
                client.cancelled = True
                raise __import__("asyncio").CancelledError()
            return await original_add(payload)

        client.add_memory = cancel_before_apply

    with pytest.raises(__import__("asyncio").CancelledError):
        await _invoke_mutation(factory, client, "add")

    if mode == "duplicates":
        only_record = next(iter(client.records.values()))
        duplicate = dict(only_record)
        duplicate["id"] = "duplicate-marked"
        client.records[duplicate["id"]] = duplicate

    result = await _recover(factory, client)
    assert result["recovered"] == 1
    marked = [
        record
        for record in client.records.values()
        if MUTATION_MARKER in record.get("metadata", {})
    ]
    assert len(marked) == 1
    if mode == "lost-response":
        assert client.add_calls == 1
    elif mode == "absent":
        assert client.add_calls == 1
    else:
        assert (
            "duplicate-marked" in client.deleted_ids
            or "added-1" in client.deleted_ids
        )
    _assert_recovered(factory, client, "add")


@pytest.mark.asyncio
async def test_lossy_sanitized_add_requires_exact_retry_with_same_marker(
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

    recovery = await _recover(factory, client)
    assert recovery == {"recovered": 1, "failed": 0}
    assert client.add_calls == 0
    with factory() as session:
        assert session.scalar(select(Event)).status.value == "FAILED"
        row = session.execute(
            text("SELECT status, result_json FROM mutation_intents")
        ).mappings().one()
        assert row["status"] == "COMPLETED"
        assert json.loads(row["result_json"])["retry_required"] is True

    with factory() as session:
        result = await MemoryService(session=session, mem0=client).add_memory(
            project_id=PROJECT_ID,
            payload=add_payload,
        )
    assert result["memory"]["id"] == "added-1"
    first_marker = seen_payloads[0]["metadata"][MUTATION_MARKER]
    retry_marker = client.records["added-1"]["metadata"][MUTATION_MARKER]
    assert retry_marker == first_marker
