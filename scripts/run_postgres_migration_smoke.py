#!/usr/bin/env python3
"""Exercise sidecar migrations and mutation fencing on disposable PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from mem0_sidecar.core.entities import EntityService
from mem0_sidecar.core.memory_ops import (
    SIDECAR_APP_ID_METADATA_KEY,
    SIDECAR_PROJECT_ID_METADATA_KEY,
    MemoryService,
    MutationConflictError,
)
from mem0_sidecar.store.models import (
    Entity,
    Event,
    MemoryIndex,
    MutationIntent,
    MutationIntentTarget,
    Project,
)
from mem0_sidecar.store.repositories import (
    EntityRepository,
    EventRepository,
    MutationIntentRepository,
)

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ID = "pg-smoke-project"
APP_ID = "pg-smoke-app"
MEMORY_ID = "pg-smoke-memory"
HEAD_EVENT_ID = "head-roundtrip-event"
HEAD_ENTITY_IDS = ("head-roundtrip-entity-a", "head-roundtrip-entity-b")
HEAD_APP_IDS = ("head-roundtrip-app-a", "head-roundtrip-app-b")


def _require(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _database_url(maintenance_url: URL, database_name: str) -> str:
    return maintenance_url.set(database=database_name).render_as_string(
        hide_password=False
    )


def _alembic_config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(ROOT))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def _migrate(config: Config, revision: str) -> None:
    previous = os.environ.pop("MEM0_SIDECAR_DATABASE_URL", None)
    try:
        command.upgrade(config, revision)
    finally:
        if previous is not None:
            os.environ["MEM0_SIDECAR_DATABASE_URL"] = previous


def _downgrade(config: Config, revision: str) -> None:
    previous = os.environ.pop("MEM0_SIDECAR_DATABASE_URL", None)
    try:
        command.downgrade(config, revision)
    finally:
        if previous is not None:
            os.environ["MEM0_SIDECAR_DATABASE_URL"] = previous


def _seed_legacy(engine) -> None:
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO projects (
                    id, name, default_app_id, mem0_base_url, settings_json,
                    created_at, updated_at
                ) VALUES (
                    :id, 'PostgreSQL smoke', :app_id, 'http://mem0.invalid',
                    '{}', :now, :now
                )
                """
            ),
            {"id": PROJECT_ID, "app_id": APP_ID, "now": now},
        )
        connection.execute(
            text(
                """
                INSERT INTO events (
                    id, project_id, operation, status, request_json,
                    response_json, error_json, created_at
                ) VALUES (
                    'legacy-event', :project_id, 'memory.list', 'SUCCEEDED',
                    '{}', '{}', '{}', :now
                )
                """
            ),
            {"project_id": PROJECT_ID, "now": now},
        )
        connection.execute(
            text(
                """
                INSERT INTO memories_index (
                    id, project_id, mem0_memory_id, user_id, app_id,
                    entity_refs_json, metadata_projection_json,
                    created_at, updated_at
                ) VALUES (
                    'legacy-memory-row', :project_id, :memory_id, 'alice',
                    :app_id, '[]', '{}', :now, :now
                )
                """
            ),
            {
                "project_id": PROJECT_ID,
                "memory_id": MEMORY_ID,
                "app_id": APP_ID,
                "now": now,
            },
        )
        for entity_id, row_id, updated_at in (
            ("alice", "legacy-entity-old", now - timedelta(days=2)),
            ("alice", "legacy-entity-new", now - timedelta(days=1)),
            ("bob", "legacy-entity-bob", now),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO entities (
                        id, project_id, entity_type, entity_id, display_name,
                        metadata_json, memory_count, last_seen_at,
                        created_at, updated_at
                    ) VALUES (
                        :id, :project_id, 'user', :entity_id, :entity_id,
                        '{}', 1, :updated_at, :updated_at, :updated_at
                    )
                    """
                ),
                {
                    "id": row_id,
                    "project_id": PROJECT_ID,
                    "entity_id": entity_id,
                    "updated_at": updated_at,
                },
            )


def _seed_head_roundtrip(engine) -> None:
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO events (
                    id, project_id, app_id, user_id, agent_id, run_id,
                    operation, status, request_json, response_json, error_json,
                    correlation_id, latency_ms, result_count, has_results,
                    created_at
                ) VALUES (
                    :id, :project_id, :app_id, 'head-user', 'head-agent',
                    'head-run', 'memory.list', 'SUCCEEDED', '{}', '{}', '{}',
                    'head-correlation', 12.5, 7, 1, :now
                )
                """
            ),
            {
                "id": HEAD_EVENT_ID,
                "project_id": PROJECT_ID,
                "app_id": HEAD_APP_IDS[1],
                "now": now,
            },
        )
        for row_id, app_id, display_name, memory_count in (
            (HEAD_ENTITY_IDS[0], HEAD_APP_IDS[0], "Head Alice A", 2),
            (HEAD_ENTITY_IDS[1], HEAD_APP_IDS[1], "Head Alice B", 3),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO entities (
                        id, project_id, app_id, entity_type, entity_id,
                        display_name, metadata_json, memory_count,
                        created_at, updated_at
                    ) VALUES (
                        :id, :project_id, :app_id, 'user', 'head-alice',
                        :display_name, '{}', :memory_count, :now, :now
                    )
                    """
                ),
                {
                    "id": row_id,
                    "project_id": PROJECT_ID,
                    "app_id": app_id,
                    "display_name": display_name,
                    "memory_count": memory_count,
                    "now": now,
                },
            )


def _index_names(inspector, table_name: str) -> set[str]:
    return {item["name"] for item in inspector.get_indexes(table_name)}


def _column_names(inspector, table_name: str) -> set[str]:
    return {item["name"] for item in inspector.get_columns(table_name)}


def _verify_head(engine) -> None:
    database = inspect(engine)
    event_columns = _column_names(database, "events")
    expected_event_columns = {
        "app_id",
        "user_id",
        "agent_id",
        "run_id",
        "correlation_id",
        "latency_ms",
        "result_count",
        "has_results",
    }
    _require(expected_event_columns <= event_columns, "0005 event columns missing")
    _require(
        {
            "ix_events_project_app_created",
            "ix_events_project_app_user_created",
            "ix_events_project_operation_created",
            "ix_events_project_status_created",
            "ix_events_project_has_results_created",
        }
        <= _index_names(database, "events"),
        "0005 request trace indexes missing",
    )
    _require("app_id" in _column_names(database, "entities"), "0006 app_id missing")
    _require(
        {
            "ix_entities_project_app_type_updated",
            "ix_entities_project_app_last_seen",
        }
        <= _index_names(database, "entities"),
        "0006 entity indexes missing",
    )
    unique_constraints = {
        item["name"] for item in database.get_unique_constraints("entities")
    }
    _require(
        "uq_entities_project_app_type_id" in unique_constraints,
        "0006 entity uniqueness missing",
    )

    for model in (
        Project,
        MemoryIndex,
        Event,
        Entity,
        MutationIntent,
        MutationIntentTarget,
    ):
        actual = _column_names(database, model.__tablename__)
        expected = set(model.__table__.columns.keys())
        _require(actual == expected, f"ORM parity failed for {model.__tablename__}")

    with Session(engine) as session:
        legacy_event = session.get(Event, "legacy-event")
        _require(legacy_event is not None, "legacy event was lost")
        _require(legacy_event.result_count == 0, "result_count backfill failed")
        _require(legacy_event.has_results == 0, "has_results backfill failed")
        entities = list(
            session.scalars(
                select(Entity)
                .where(
                    Entity.project_id == PROJECT_ID,
                    Entity.id.in_(
                        [
                            "legacy-entity-new",
                            "legacy-entity-bob",
                        ]
                    ),
                )
                .order_by(Entity.entity_id)
            )
        )
        _require(len(entities) == 2, "entity dedupe did not remove one duplicate")
        alice = next(item for item in entities if item.entity_id == "alice")
        _require(alice.id == "legacy-entity-new", "entity dedupe was not deterministic")
        _require(
            all(item.app_id == APP_ID for item in entities),
            "entity app_id backfill failed",
        )
        session.add(
            Event(
                project_id=PROJECT_ID,
                app_id=APP_ID,
                operation="smoke.usability",
                status="SUCCEEDED",
            )
        )
        session.commit()
        _require(
            session.scalar(
                select(Event).where(Event.operation == "smoke.usability")
            )
            is not None,
            "head ORM data usability failed",
        )


def _verify_downgraded_0004(engine) -> None:
    database = inspect(engine)
    _require(
        "app_id" not in _column_names(database, "entities"),
        "0006 app_id survived downgrade",
    )
    _require(
        "result_count" not in _column_names(database, "events"),
        "0005 result_count survived downgrade",
    )
    _require(
        not any(
            name.startswith("ix_events_project_")
            for name in _index_names(database, "events")
        ),
        "0005 indexes survived downgrade",
    )
    _require(
        database.has_table("_compat_0005_request_trace_fields"),
        "0005 compatibility table missing after downgrade",
    )
    _require(
        database.has_table("_compat_0006_entity_projection_scope"),
        "0006 compatibility table missing after downgrade",
    )
    with engine.connect() as connection:
        _require(
            connection.scalar(
                text("SELECT count(*) FROM events WHERE id = 'legacy-event'")
            )
            == 1,
            "legacy event unusable after downgrade",
        )
        _require(
            connection.scalar(
                text("SELECT count(*) FROM entities WHERE project_id = :project_id"),
                {"project_id": PROJECT_ID},
            )
            == 4,
            "deduped entities unusable after downgrade",
        )
        _require(
            connection.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM _compat_0005_request_trace_fields
                    WHERE event_id = :event_id
                    """
                ),
                {"event_id": HEAD_EVENT_ID},
            )
            == 1,
            "head request trace compatibility row was lost",
        )
        _require(
            connection.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM _compat_0006_entity_projection_scope
                    WHERE entity_id IN (:entity_a, :entity_b)
                    """
                ),
                {
                    "entity_a": HEAD_ENTITY_IDS[0],
                    "entity_b": HEAD_ENTITY_IDS[1],
                },
            )
            == 2,
            "head entity compatibility rows were lost",
        )


def _verify_head_roundtrip(engine) -> None:
    database = inspect(engine)
    _require(
        not database.has_table("_compat_0005_request_trace_fields"),
        "0005 compatibility table survived successful restoration",
    )
    _require(
        not database.has_table("_compat_0006_entity_projection_scope"),
        "0006 compatibility table survived successful restoration",
    )
    with engine.connect() as connection:
        event = connection.execute(
            text(
                """
                SELECT app_id, user_id, agent_id, run_id, correlation_id,
                       latency_ms, result_count, has_results
                FROM events WHERE id = :event_id
                """
            ),
            {"event_id": HEAD_EVENT_ID},
        ).mappings().one()
        entities = connection.execute(
            text(
                """
                SELECT id, app_id, display_name, memory_count
                FROM entities
                WHERE id IN (:entity_a, :entity_b)
                ORDER BY id
                """
            ),
            {
                "entity_a": HEAD_ENTITY_IDS[0],
                "entity_b": HEAD_ENTITY_IDS[1],
            },
        ).mappings().all()

    _require(
        dict(event)
        == {
            "app_id": HEAD_APP_IDS[1],
            "user_id": "head-user",
            "agent_id": "head-agent",
            "run_id": "head-run",
            "correlation_id": "head-correlation",
            "latency_ms": 12.5,
            "result_count": 7,
            "has_results": 1,
        },
        "head request trace did not survive exact roundtrip",
    )
    _require(
        [dict(item) for item in entities]
        == [
            {
                "id": HEAD_ENTITY_IDS[0],
                "app_id": HEAD_APP_IDS[0],
                "display_name": "Head Alice A",
                "memory_count": 2,
            },
            {
                "id": HEAD_ENTITY_IDS[1],
                "app_id": HEAD_APP_IDS[1],
                "display_name": "Head Alice B",
                "memory_count": 3,
            },
        ],
        "head multi-app entities did not survive exact roundtrip",
    )


class _BlockingUpdateClient:
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self.started = started
        self.release = release

    async def update_memory(self, memory_id, payload):
        self.started.set()
        if not self.release.wait(8):
            raise TimeoutError("update smoke release timed out")
        return {"id": memory_id, "updated": True}

    async def get_memory(self, memory_id):
        return {
            "id": memory_id,
            "memory": "updated",
            "user_id": "alice",
            "app_id": APP_ID,
            "metadata": {
                SIDECAR_PROJECT_ID_METADATA_KEY: PROJECT_ID,
                SIDECAR_APP_ID_METADATA_KEY: APP_ID,
            },
        }


class _BlockingReconcileClient:
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self.started = started
        self.release = release

    async def list_memories(self, params):
        self.started.set()
        if not self.release.wait(8):
            raise TimeoutError("reconcile smoke release timed out")
        return {
            "results": [
                {
                    "id": MEMORY_ID,
                    "memory": "reconciled",
                    "user_id": "alice",
                    "app_id": APP_ID,
                    "metadata": {
                        SIDECAR_PROJECT_ID_METADATA_KEY: PROJECT_ID,
                        SIDECAR_APP_ID_METADATA_KEY: APP_ID,
                    },
                }
            ],
            "total": 1,
        }


class _DeleteClient:
    def __init__(self, called: threading.Event) -> None:
        self.called = called

    async def delete_memory(self, memory_id):
        self.called.set()
        return {"id": memory_id, "deleted": True}


class _BlockingAdminObservationClient:
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self.started = started
        self.release = release
        self.read_calls = 0
        self.write_calls = 0

    async def list_memories(self, params):
        self.read_calls += 1
        self.started.set()
        if not self.release.wait(8):
            raise TimeoutError("admin marker observation release timed out")
        return {"results": [], "total": 0}


def _run_admin_resolution_interleaving(session_factory) -> None:
    try:
        from mem0_sidecar.core.mutation_admin import MutationAdminService
    except ModuleNotFoundError as exc:
        raise AssertionError(
            "mutation admin service is required for PostgreSQL serialization"
        ) from exc

    _reset_projection(session_factory)
    marker = "pg-admin-marker"
    with session_factory() as session:
        event = EventRepository(session).create_event(
            project_id=PROJECT_ID,
            app_id=APP_ID,
            operation="memory.add",
            request={"app_id": APP_ID, "text": "ambiguous"},
            subject_type="memory",
        )
        intent = MutationIntentRepository(session).create(
            project_id=PROJECT_ID,
            app_id=APP_ID,
            event_id=event.id,
            operation="memory.add",
            operation_key="pg-admin-operation-key",
            payload={"mutation_id": marker},
        )
        intent.status = "EXHAUSTED"
        intent.attempt_count = 3
        intent.lease_expires_at = None
        session.commit()
        intent_id = intent.id

    started = threading.Event()
    release = threading.Event()
    client = _BlockingAdminObservationClient(started, release)
    resolution_result: list[dict] = []
    recovery_result: list[dict] = []
    recovery_conflicts: list[str] = []
    failures: list[BaseException] = []

    def resolve() -> None:
        try:
            with session_factory() as session:
                resolution_result.append(
                    asyncio.run(
                        MutationAdminService(
                            session=session,
                            mem0=client,
                        ).resolve_intent(
                            project_id=PROJECT_ID,
                            app_id=APP_ID,
                            intent_id=intent_id,
                            confirmation_intent_id=intent_id,
                            expected_status="EXHAUSTED",
                            expected_attempt_count=3,
                            reason="PostgreSQL serialization acceptance",
                            accept_unknown_outcome=True,
                        )
                    )
                )
        except BaseException as exc:
            failures.append(exc)

    def recover() -> None:
        try:
            with session_factory() as session:
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
        except MutationConflictError as exc:
            recovery_conflicts.append(str(exc))
        except BaseException as exc:
            failures.append(exc)

    resolution_thread = threading.Thread(target=resolve, daemon=True)
    recovery_thread = threading.Thread(target=recover, daemon=True)
    resolution_thread.start()
    _require(started.wait(5), "admin resolution never observed the add marker")
    recovery_thread.start()
    recovery_thread.join(0.5)
    progressed_during_observation = not recovery_thread.is_alive()
    _require(
        progressed_during_observation,
        "PostgreSQL recovery was blocked by admin marker observation",
    )
    release.set()
    resolution_thread.join(10)
    recovery_thread.join(10)

    _require(not resolution_thread.is_alive(), "admin resolution deadlocked")
    _require(not recovery_thread.is_alive(), "recovery deadlocked")
    _require(not failures, f"admin resolution concurrency failures: {failures!r}")
    _require(resolution_result, "admin resolution did not complete")
    _require(
        recovery_result == [] and len(recovery_conflicts) == 1,
        "recovery did not observe the still-blocking exhausted intent",
    )
    _require(client.read_calls == 1, "admin resolution repeated marker observation")
    _require(client.write_calls == 0, "admin resolution issued an upstream write")
    with session_factory() as session:
        resolved = session.get(MutationIntent, intent_id)
        _require(
            resolved is not None and resolved.status == "FAILED",
            "admin resolution did not terminalize the original intent",
        )
        audit_count = session.scalar(
            select(func.count())
            .select_from(Event)
            .where(Event.operation == "mutation.resolve")
        )
        _require(audit_count == 1, "admin resolution audit event is missing")


def _compat_migration(revision: str):
    module_name = (
        "migrations.versions.0005_request_trace_fields"
        if revision == "0005"
        else "migrations.versions.0006_entity_projection_scope"
    )
    return importlib.import_module(module_name)


def _compat_revision_name(revision: str) -> str:
    return (
        "0005_request_trace_fields"
        if revision == "0005"
        else "0006_entity_projection_scope"
    )


def _compat_down_revision(revision: str) -> str:
    return (
        "0004_memory_explorer_indexes"
        if revision == "0005"
        else "0005_request_trace_fields"
    )


def _insert_compat_source_row(connection, revision: str, row_id: str) -> None:
    if revision == "0005":
        connection.execute(
            text(
                """
                INSERT INTO events (
                    id, project_id, app_id, user_id, agent_id, run_id,
                    operation, status, request_json, response_json, error_json,
                    correlation_id, latency_ms, result_count, has_results,
                    created_at
                ) VALUES (
                    :row_id, :project_id, 'pg-writer-app', 'pg-writer-user',
                    'pg-writer-agent', 'pg-writer-run', 'memory.search',
                    'SUCCEEDED', '{}', '{}', '{}', 'pg-writer-correlation',
                    17.25, 9, 1, :now
                )
                """
            ),
            {
                "row_id": row_id,
                "project_id": PROJECT_ID,
                "now": datetime.now(UTC),
            },
        )
        return
    connection.execute(
        text(
            """
            INSERT INTO entities (
                id, project_id, app_id, entity_type, entity_id,
                display_name, metadata_json, memory_count,
                created_at, updated_at
            ) VALUES (
                :row_id, :project_id, 'pg-writer-app', 'user', :row_id,
                'PG Writer Entity', '{}', 4, :now, :now
            )
            """
        ),
        {
            "row_id": row_id,
            "project_id": PROJECT_ID,
            "now": datetime.now(UTC),
        },
    )


def _compat_source_values(connection, revision: str, row_id: str):
    if revision == "0005":
        return connection.execute(
            text(
                """
                SELECT app_id, user_id, agent_id, run_id, correlation_id,
                       latency_ms, result_count, has_results
                FROM events WHERE id = :row_id
                """
            ),
            {"row_id": row_id},
        ).mappings().one_or_none()
    return connection.execute(
        text(
            """
            SELECT app_id, display_name, memory_count
            FROM entities WHERE id = :row_id
            """
        ),
        {"row_id": row_id},
    ).mappings().one_or_none()


def _expected_compat_source_values(revision: str):
    if revision == "0005":
        return {
            "app_id": "pg-writer-app",
            "user_id": "pg-writer-user",
            "agent_id": "pg-writer-agent",
            "run_id": "pg-writer-run",
            "correlation_id": "pg-writer-correlation",
            "latency_ms": 17.25,
            "result_count": 9,
            "has_results": 1,
        }
    return {
        "app_id": "pg-writer-app",
        "display_name": "PG Writer Entity",
        "memory_count": 4,
    }


def _run_direct_compat_downgrade(engine, migration, target_revision: str) -> None:
    original_op = migration.op
    try:
        with engine.connect() as connection:
            with connection.begin():
                migration.op = Operations(MigrationContext.configure(connection))
                migration.downgrade()
                connection.execute(
                    text("UPDATE alembic_version SET version_num = :revision"),
                    {"revision": target_revision},
                )
    finally:
        migration.op = original_op


def _run_compat_writer_first_schedule(engine, config: Config, revision: str) -> str:
    _downgrade(config, _compat_revision_name(revision))
    migration = _compat_migration(revision)
    row_id = f"pg-{revision}-writer-first"
    writer_inserted = threading.Event()
    release_writer = threading.Event()
    writer_committed = threading.Event()
    snapshot_started = threading.Event()
    snapshot_finished = threading.Event()
    writer_errors: list[BaseException] = []
    migration_errors: list[BaseException] = []
    original_rebuild = migration._rebuild_compatibility_snapshot

    def observed_rebuild() -> None:
        snapshot_started.set()
        original_rebuild()
        snapshot_finished.set()

    migration._rebuild_compatibility_snapshot = observed_rebuild

    def writer() -> None:
        try:
            with engine.connect() as connection:
                transaction = connection.begin()
                _insert_compat_source_row(connection, revision, row_id)
                writer_inserted.set()
                _require(
                    release_writer.wait(8),
                    f"{revision} writer-first release timed out",
                )
                transaction.commit()
                writer_committed.set()
        except BaseException as exc:
            writer_errors.append(exc)

    def downgrade() -> None:
        try:
            _run_direct_compat_downgrade(
                engine,
                migration,
                _compat_down_revision(revision),
            )
        except BaseException as exc:
            migration_errors.append(exc)

    writer_thread = threading.Thread(target=writer, daemon=True)
    migration_thread = threading.Thread(target=downgrade, daemon=True)
    try:
        writer_thread.start()
        _require(writer_inserted.wait(5), f"{revision} writer did not insert")
        migration_thread.start()
        _require(snapshot_started.wait(5), f"{revision} snapshot did not start")
        snapshot_finished_before_commit = snapshot_finished.wait(0.5)
        release_writer.set()
        writer_thread.join(10)
        migration_thread.join(10)
    finally:
        release_writer.set()
        migration._rebuild_compatibility_snapshot = original_rebuild

    _require(not writer_thread.is_alive(), f"{revision} writer-first deadlocked")
    _require(not migration_thread.is_alive(), f"{revision} migration deadlocked")
    _require(not writer_errors, f"{revision} writer errors: {writer_errors!r}")
    _require(not migration_errors, f"{revision} migration errors: {migration_errors!r}")
    _require(writer_committed.is_set(), f"{revision} earlier writer did not commit")
    _migrate(config, "head")
    with engine.connect() as connection:
        values = _compat_source_values(connection, revision, row_id)
    expected = _expected_compat_source_values(revision)
    if snapshot_finished_before_commit or values != expected:
        return (
            f"{revision} writer-first snapshot_before_commit="
            f"{snapshot_finished_before_commit} values={dict(values or {})!r}"
        )
    return ""


def _run_compat_migration_first_schedule(engine, config: Config, revision: str) -> str:
    _downgrade(config, _compat_revision_name(revision))
    migration = _compat_migration(revision)
    row_id = f"pg-{revision}-migration-first"
    snapshot_ready = threading.Event()
    release_snapshot = threading.Event()
    writer_attempted = threading.Event()
    writer_committed = threading.Event()
    writer_errors: list[BaseException] = []
    migration_errors: list[BaseException] = []
    original_validate = migration._validate_compatibility_snapshot

    def pause_ready_snapshot(*, expected_source_rows=None, require_ready=True):
        result = original_validate(
            expected_source_rows=expected_source_rows,
            require_ready=require_ready,
        )
        if require_ready:
            snapshot_ready.set()
            _require(
                release_snapshot.wait(8),
                f"{revision} migration-first release timed out",
            )
        return result

    migration._validate_compatibility_snapshot = pause_ready_snapshot

    def downgrade() -> None:
        try:
            _run_direct_compat_downgrade(
                engine,
                migration,
                _compat_down_revision(revision),
            )
        except BaseException as exc:
            migration_errors.append(exc)

    def writer() -> None:
        try:
            with engine.begin() as connection:
                writer_attempted.set()
                _insert_compat_source_row(connection, revision, row_id)
            writer_committed.set()
        except BaseException as exc:
            writer_errors.append(exc)

    migration_thread = threading.Thread(target=downgrade, daemon=True)
    writer_thread = threading.Thread(target=writer, daemon=True)
    try:
        migration_thread.start()
        _require(snapshot_ready.wait(5), f"{revision} snapshot was not ready")
        writer_thread.start()
        _require(writer_attempted.wait(5), f"{revision} writer did not attempt")
        writer_committed_before_release = writer_committed.wait(0.5)
        release_snapshot.set()
        migration_thread.join(10)
        writer_thread.join(10)
    finally:
        release_snapshot.set()
        migration._validate_compatibility_snapshot = original_validate

    _require(not migration_thread.is_alive(), f"{revision} migration deadlocked")
    _require(not writer_thread.is_alive(), f"{revision} late writer deadlocked")
    _require(not migration_errors, f"{revision} migration errors: {migration_errors!r}")
    _migrate(config, "head")
    with engine.connect() as connection:
        values = _compat_source_values(connection, revision, row_id)
    if writer_committed_before_release:
        expected = _expected_compat_source_values(revision)
        return (
            f"{revision} migration-first committed_before_release=True "
            f"expected={expected!r} values={dict(values or {})!r}"
        )
    if writer_committed.is_set() or values is not None or not writer_errors:
        return (
            f"{revision} migration-first late writer outcome was unsafe: "
            f"committed={writer_committed.is_set()} values={dict(values or {})!r} "
            f"errors={writer_errors!r}"
        )
    return ""


def _verify_compat_snapshot_serialization(engine, config: Config) -> None:
    failures = []
    for revision in ("0005", "0006"):
        writer_first = _run_compat_writer_first_schedule(engine, config, revision)
        if writer_first:
            failures.append(writer_first)
        migration_first = _run_compat_migration_first_schedule(
            engine,
            config,
            revision,
        )
        if migration_first:
            failures.append(migration_first)
    _require(
        not failures,
        "compatibility snapshot writer fences failed: " + "; ".join(failures),
    )


def _reset_projection(session_factory) -> None:
    with session_factory() as session:
        session.query(MutationIntent).filter(
            MutationIntent.project_id == PROJECT_ID
        ).delete()
        session.query(Event).filter(Event.project_id == PROJECT_ID).delete()
        session.query(Entity).filter(Entity.project_id == PROJECT_ID).delete()
        session.query(MemoryIndex).filter(
            MemoryIndex.project_id == PROJECT_ID
        ).delete()
        session.add(
            MemoryIndex(
                project_id=PROJECT_ID,
                mem0_memory_id=MEMORY_ID,
                user_id="alice",
                app_id=APP_ID,
                metadata_projection_json="{}",
            )
        )
        session.flush()
        EntityRepository(session).rebuild_project_entities(PROJECT_ID, APP_ID)
        session.commit()


def _run_interleaving(session_factory, operation: str) -> None:
    _reset_projection(session_factory)
    started = threading.Event()
    release = threading.Event()
    delete_called = threading.Event()
    mutation_failures: list[BaseException] = []
    delete_conflicts: list[str] = []
    delete_failures: list[BaseException] = []

    def mutate() -> None:
        try:
            with session_factory() as session:
                if operation == "update":
                    asyncio.run(
                        MemoryService(
                            session=session,
                            mem0=_BlockingUpdateClient(started, release),
                        ).update_memory(
                            project_id=PROJECT_ID,
                            memory_id=MEMORY_ID,
                            request_app_id=APP_ID,
                            payload={"text": "updated"},
                        )
                    )
                else:
                    asyncio.run(
                        MemoryService(
                            session=session,
                            mem0=_BlockingReconcileClient(started, release),
                        ).reconcile_memories(
                            project_id=PROJECT_ID,
                            app_id=APP_ID,
                            adopt_unscoped=False,
                            allow_adopt_unscoped=False,
                            default_project_id=PROJECT_ID,
                        )
                    )
                session.commit()
        except BaseException as exc:
            mutation_failures.append(exc)

    def delete_entity() -> None:
        try:
            with session_factory() as session:
                asyncio.run(
                    EntityService(
                        session=session,
                        mem0=_DeleteClient(delete_called),
                    ).delete_entity(PROJECT_ID, APP_ID, "user", "alice")
                )
                session.commit()
        except MutationConflictError as exc:
            delete_conflicts.append(str(exc))
        except BaseException as exc:
            delete_failures.append(exc)

    mutation_thread = threading.Thread(target=mutate, daemon=True)
    delete_thread = threading.Thread(target=delete_entity, daemon=True)
    mutation_thread.start()
    _require(started.wait(5), f"{operation} never reached its upstream call")
    delete_thread.start()
    delete_thread.join(0.5)
    _require(
        not delete_thread.is_alive(),
        f"entity delete waited on the {operation} upstream call",
    )
    release.set()
    mutation_thread.join(10)
    delete_thread.join(10)
    _require(not mutation_thread.is_alive(), f"{operation} deadlocked")
    _require(not delete_thread.is_alive(), "entity delete deadlocked")
    _require(not mutation_failures, f"mutation failures: {mutation_failures!r}")
    _require(not delete_failures, f"entity delete failures: {delete_failures!r}")
    _require(len(delete_conflicts) == 1, "entity delete bypassed the active intent")
    _require(not delete_called.is_set(), "blocked entity delete reached upstream")
    with session_factory() as session:
        projection = session.scalar(
            select(MemoryIndex).where(
                MemoryIndex.project_id == PROJECT_ID,
                MemoryIndex.mem0_memory_id == MEMORY_ID,
            )
        )
        _require(
            projection is not None and projection.deleted_at is None,
            f"{operation} lost the active memory projection",
        )
        _require(
            session.scalar(
                select(Entity).where(
                    Entity.project_id == PROJECT_ID,
                    Entity.app_id == APP_ID,
                    Entity.entity_type == "user",
                    Entity.entity_id == "alice",
                )
            )
            is not None,
            f"{operation} lost the active entity projection",
        )


def _verify_intent_downgrade_guard(engine, config: Config) -> None:
    now = datetime.now(UTC)
    head_revision = ScriptDirectory.from_config(config).get_current_head()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO events (
                    id, project_id, app_id, operation, status,
                    request_json, response_json, error_json, created_at
                ) VALUES (
                    'pg-guard-event', :project_id, :app_id,
                    'memory.delete', 'FAILED', '{}', '{}', '{}', :now
                )
                """
            ),
            {"project_id": PROJECT_ID, "app_id": APP_ID, "now": now},
        )
        connection.execute(
            text(
                """
                INSERT INTO mutation_intents (
                    id, project_id, app_id, event_id, operation, operation_key,
                    status, payload_json, result_json, error_json, attempt_count,
                    created_at, updated_at
                ) VALUES (
                    'pg-guard-intent', :project_id, :app_id, 'pg-guard-event',
                    'memory.delete', 'pg-guard-key', 'UNKNOWN', '{}', '{}', '{}', 2,
                    :now, :now
                )
                """
            ),
            {"project_id": PROJECT_ID, "app_id": APP_ID, "now": now},
        )
        connection.execute(
            text(
                """
                INSERT INTO mutation_intent_targets (
                    id, intent_id, memory_id, ordinal, status, error_json,
                    created_at, updated_at
                ) VALUES (
                    'pg-guard-target', 'pg-guard-intent', :memory_id,
                    0, 'PENDING', '{}', :now, :now
                )
                """
            ),
            {"memory_id": MEMORY_ID, "now": now},
        )

    try:
        _downgrade(config, "0006_entity_projection_scope")
    except RuntimeError as exc:
        _require(
            "nonterminal mutation intents" in str(exc),
            f"unexpected 0007 downgrade refusal: {exc}",
        )
    else:
        raise AssertionError("0007 downgrade accepted an UNKNOWN intent")

    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        _require(
            {"mutation_intents", "mutation_intent_targets"}.issubset(tables),
            "0007 downgrade refusal dropped intent tables",
        )
        _require(
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM mutation_intents "
                    "WHERE id = 'pg-guard-intent' AND status = 'UNKNOWN'"
                )
            )
            == 1,
            "0007 downgrade refusal changed the unresolved intent",
        )
        _require(
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM mutation_intent_targets "
                    "WHERE id = 'pg-guard-target'"
                )
            )
            == 1,
            "0007 downgrade refusal changed the target row",
        )
        _require(
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == head_revision,
            "0007 downgrade refusal changed the transactional Alembic revision",
        )

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE mutation_intents
                SET status = 'FAILED', completed_at = :now
                WHERE id = 'pg-guard-intent'
                """
            ),
            {"now": now},
        )
    _downgrade(config, "0006_entity_projection_scope")
    _migrate(config, "head")


def _seed_interrupted_compatibility_artifacts(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE _compat_0005_request_trace_fields (
                    event_id VARCHAR(36) PRIMARY KEY,
                    app_id VARCHAR(256), user_id VARCHAR(256),
                    agent_id VARCHAR(256), run_id VARCHAR(256),
                    correlation_id VARCHAR(256), latency_ms DOUBLE PRECISION,
                    result_count BIGINT NOT NULL, has_results INTEGER NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE _compat_0006_entity_projection_scope (
                    entity_id VARCHAR(36) PRIMARY KEY,
                    app_id VARCHAR(256) NOT NULL
                )
                """
            )
        )


def _convert_ready_artifacts_to_exact_b502a26_legacy(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                ALTER TABLE _compat_0005_request_trace_fields
                RENAME TO _ready_0005_request_trace_fields
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE _compat_0005_request_trace_fields (
                    event_id VARCHAR(36) PRIMARY KEY,
                    app_id VARCHAR(256), user_id VARCHAR(256),
                    agent_id VARCHAR(256), run_id VARCHAR(256),
                    correlation_id VARCHAR(256), latency_ms DOUBLE PRECISION,
                    result_count BIGINT NOT NULL, has_results INTEGER NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO _compat_0005_request_trace_fields (
                    event_id, app_id, user_id, agent_id, run_id,
                    correlation_id, latency_ms, result_count, has_results
                )
                SELECT
                    event_id, app_id, user_id, agent_id, run_id,
                    correlation_id, latency_ms, result_count, has_results
                FROM _ready_0005_request_trace_fields
                WHERE snapshot_kind = 'DATA'
                """
            )
        )
        connection.execute(text("DROP TABLE _ready_0005_request_trace_fields"))
        connection.execute(
            text(
                """
                ALTER TABLE _compat_0006_entity_projection_scope
                RENAME TO _ready_0006_entity_projection_scope
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE _compat_0006_entity_projection_scope (
                    entity_id VARCHAR(36) PRIMARY KEY,
                    app_id VARCHAR(256) NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO _compat_0006_entity_projection_scope (
                    entity_id, app_id
                )
                SELECT entity_id, app_id
                FROM _ready_0006_entity_projection_scope
                WHERE snapshot_kind = 'DATA'
                """
            )
        )
        connection.execute(text("DROP TABLE _ready_0006_entity_projection_scope"))


def _run(database_url: str) -> None:
    config = _alembic_config(database_url)
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        _migrate(config, "0004_memory_explorer_indexes")
        _seed_legacy(engine)
        _migrate(config, "head")
        _verify_head(engine)
        _seed_head_roundtrip(engine)
        _verify_intent_downgrade_guard(engine, config)
        _seed_interrupted_compatibility_artifacts(engine)
        _downgrade(config, "0004_memory_explorer_indexes")
        _convert_ready_artifacts_to_exact_b502a26_legacy(engine)
        _verify_downgraded_0004(engine)
        _migrate(config, "head")
        _verify_head(engine)
        _verify_head_roundtrip(engine)
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        _run_interleaving(session_factory, "update")
        _run_interleaving(session_factory, "reconcile")
        _run_admin_resolution_interleaving(session_factory)
        _verify_compat_snapshot_serialization(engine, config)
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args()
    maintenance_url = make_url(args.database_url)
    database_name = f"sidecar_smoke_{uuid4().hex}"
    maintenance_engine = create_engine(
        maintenance_url,
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )
    quoted_database = maintenance_engine.dialect.identifier_preparer.quote(
        database_name
    )
    try:
        with maintenance_engine.connect() as connection:
            connection.execute(text(f"CREATE DATABASE {quoted_database}"))
        _run(_database_url(maintenance_url, database_name))
        print(
            "PostgreSQL smoke passed: 0004->head, interruption-safe and "
            "b502a26-legacy exact downgrade/re-upgrade, 0007 locked "
            "unresolved-intent refusal, ORM/data checks, update/delete and "
            "reconcile/delete serialization, admin/recovery serialization, "
            "0005/0006 compatibility snapshot serialization"
        )
    finally:
        with maintenance_engine.connect() as connection:
            connection.execute(
                text(
                    """
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = :database_name AND pid <> pg_backend_pid()
                    """
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f"DROP DATABASE IF EXISTS {quoted_database}"))
        maintenance_engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
