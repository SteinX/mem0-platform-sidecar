import importlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.dialects import postgresql

PROJECT_ROOT = Path(__file__).resolve().parents[2]

MEMORY_EXPLORER_INDEXES = {
    "ix_memories_index_project_active_created",
    "ix_memories_index_project_app_user",
    "ix_memories_index_project_app_agent",
    "ix_memories_index_project_app_run",
    "ix_memories_index_project_category",
}

REQUEST_TRACE_INDEXES = {
    "ix_events_project_created",
    "ix_events_project_app_created",
    "ix_events_project_app_user_created",
    "ix_events_project_app_agent_created",
    "ix_events_project_app_run_created",
    "ix_events_project_operation_created",
    "ix_events_project_status_created",
    "ix_events_project_has_results_created",
}
REQUEST_TRACE_INDEX_COLUMNS = {
    "ix_events_project_app_created": ("project_id", "app_id", "created_at"),
    "ix_events_project_app_user_created": (
        "project_id",
        "app_id",
        "user_id",
        "created_at",
    ),
    "ix_events_project_app_agent_created": (
        "project_id",
        "app_id",
        "agent_id",
        "created_at",
    ),
    "ix_events_project_app_run_created": (
        "project_id",
        "app_id",
        "run_id",
        "created_at",
    ),
}

ENTITY_PROJECTION_INDEX_COLUMNS = {
    "ix_entities_project_app_type_updated": (
        "project_id",
        "app_id",
        "entity_type",
        "updated_at",
    ),
    "ix_entities_project_app_last_seen": (
        "project_id",
        "app_id",
        "last_seen_at",
    ),
}


def _alembic_config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.set_main_option("path_separator", "os")
    return config


def test_export_jobs_migration_supports_runtime_defaults_and_downgrade(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'alembic.sqlite3'}"
    config = _alembic_config(database_url)

    command.upgrade(config, "head")

    engine = sa.create_engine(database_url, future=True)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO projects (
                    id,
                    name,
                    mem0_base_url,
                    created_at,
                    updated_at
                ) VALUES (
                    :id,
                    :name,
                    :mem0_base_url,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
                """
            ),
            {
                "id": "repo-a",
                "name": "Repo A",
                "mem0_base_url": "http://mem0:8000",
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO export_jobs (
                    id,
                    project_id,
                    format,
                    created_at
                ) VALUES (
                    :id,
                    :project_id,
                    :format,
                    CURRENT_TIMESTAMP
                )
                """
            ),
            {
                "id": "job-1",
                "project_id": "repo-a",
                "format": "json",
            },
        )
        row = connection.execute(
            sa.text(
                """
                SELECT
                    status,
                    filters_json,
                    result_json,
                    error_json,
                    total_count,
                    exported_count,
                    skipped_count
                FROM export_jobs
                WHERE id = :id
                """
            ),
            {"id": "job-1"},
        ).mappings().one()

    assert row["status"] == "PENDING"
    assert row["filters_json"] == "{}"
    assert row["result_json"] == "{}"
    assert row["error_json"] == "{}"
    assert row["total_count"] == 0
    assert row["exported_count"] == 0
    assert row["skipped_count"] == 0

    command.downgrade(config, "base")

    inspector = sa.inspect(sa.create_engine(database_url, future=True))
    assert "export_jobs" not in inspector.get_table_names()


def test_export_jobs_migration_does_not_precreate_postgres_enum(monkeypatch) -> None:
    migration = importlib.import_module("migrations.versions.0002_export_jobs")
    created_tables: list[str] = []

    def create_table(name: str, *args, **kwargs) -> None:
        created_tables.append(name)

    def fail_manual_enum_create(*args, **kwargs) -> None:
        raise AssertionError("exportstatus should be created by create_table")

    monkeypatch.setattr(
        migration,
        "op",
        SimpleNamespace(
            get_bind=lambda: SimpleNamespace(
                dialect=SimpleNamespace(name="postgresql")
            ),
            create_table=create_table,
        ),
    )
    monkeypatch.setattr(migration.export_status, "create", fail_manual_enum_create)

    migration.upgrade()

    assert created_tables == ["export_jobs"]


def test_category_name_uniqueness_migration_upgrades_and_downgrades_sqlite(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'category-uniqueness.sqlite3'}"
    config = _alembic_config(database_url)
    command.upgrade(config, "0002_export_jobs")
    engine = sa.create_engine(database_url, future=True)

    with engine.begin() as connection:
        for project_id in ("alpha", "beta"):
            connection.execute(
                sa.text(
                    """
                    INSERT INTO projects (
                        id, name, mem0_base_url, created_at, updated_at
                    ) VALUES (
                        :id, :id, 'http://mem0:8000', CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP
                    )
                    """
                ),
                {"id": project_id},
            )

    command.upgrade(config, "head")

    constraints = sa.inspect(engine).get_unique_constraints("categories")
    assert {
        (constraint["name"], tuple(constraint["column_names"]))
        for constraint in constraints
    } == {("uq_categories_project_id_name", ("project_id", "name"))}

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO categories (
                    id, project_id, name, created_at, updated_at
                ) VALUES (
                    :id, :project_id, 'work', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            ),
            {"id": "category-alpha", "project_id": "alpha"},
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO categories (
                    id, project_id, name, created_at, updated_at
                ) VALUES (
                    :id, :project_id, 'work', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            ),
            {"id": "category-beta", "project_id": "beta"},
        )
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                sa.text(
                    """
                    INSERT INTO categories (
                        id, project_id, name, created_at, updated_at
                    ) VALUES (
                        'category-duplicate', 'alpha', 'work',
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
            )

    command.downgrade(config, "0002_export_jobs")
    assert sa.inspect(engine).get_unique_constraints("categories") == []

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO categories (
                    id, project_id, name, created_at, updated_at
                ) VALUES (
                    'category-after-downgrade', 'alpha', 'work',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )


def test_category_name_uniqueness_migration_rejects_existing_duplicates_cleanly(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'category-duplicates.sqlite3'}"
    config = _alembic_config(database_url)
    command.upgrade(config, "0002_export_jobs")
    engine = sa.create_engine(database_url, future=True)

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO projects (
                    id, name, mem0_base_url, created_at, updated_at
                ) VALUES (
                    'default', 'default', 'http://mem0:8000',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        for category_id in ("category-one", "category-two"):
            connection.execute(
                sa.text(
                    """
                    INSERT INTO categories (
                        id, project_id, name, created_at, updated_at
                    ) VALUES (
                        :id, 'default', 'duplicate',
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {"id": category_id},
            )

    with pytest.raises(
        RuntimeError,
        match="duplicate category names per project already exist",
    ):
        command.upgrade(config, "head")

    with engine.connect() as connection:
        assert connection.scalar(
            sa.text("SELECT version_num FROM alembic_version")
        ) == "0002_export_jobs"
        assert "_alembic_tmp_categories" not in sa.inspect(
            connection
        ).get_table_names()


def test_category_name_uniqueness_migration_uses_batch_operations(monkeypatch) -> None:
    migration = importlib.import_module(
        "migrations.versions.0003_category_name_uniqueness"
    )
    operations: list[tuple[str, str, tuple[str, ...]]] = []

    class BatchOperations:
        def create_unique_constraint(self, name: str, columns: list[str]) -> None:
            operations.append(("create", name, tuple(columns)))

        def drop_constraint(self, name: str, type_: str) -> None:
            operations.append((f"drop:{type_}", name, ()))

    @contextmanager
    def batch_alter_table(table_name: str):
        assert table_name == "categories"
        yield BatchOperations()

    monkeypatch.setattr(
        migration,
        "op",
        SimpleNamespace(
            batch_alter_table=batch_alter_table,
            get_bind=lambda: SimpleNamespace(
                execute=lambda statement: SimpleNamespace(first=lambda: None)
            ),
        ),
    )

    migration.upgrade()
    migration.downgrade()

    assert operations == [
        ("create", "uq_categories_project_id_name", ("project_id", "name")),
        ("drop:unique", "uq_categories_project_id_name", ()),
    ]


def test_memory_explorer_indexes_migration_upgrades_and_downgrades(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'memory-explorer-indexes.sqlite3'}"
    config = _alembic_config(database_url)

    command.upgrade(config, "head")

    engine = sa.create_engine(database_url, future=True)
    upgraded_indexes = {
        index["name"] for index in sa.inspect(engine).get_indexes("memories_index")
    }
    assert MEMORY_EXPLORER_INDEXES <= upgraded_indexes

    command.downgrade(config, "0003_category_name_uniqueness")

    downgraded_indexes = {
        index["name"] for index in sa.inspect(engine).get_indexes("memories_index")
    }
    assert MEMORY_EXPLORER_INDEXES.isdisjoint(downgraded_indexes)


def test_request_trace_migration_upgrades_legacy_rows_and_downgrades(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'request-traces.sqlite3'}"
    config = _alembic_config(database_url)
    command.upgrade(config, "0004_memory_explorer_indexes")
    engine = sa.create_engine(database_url, future=True)

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO projects (
                    id, name, mem0_base_url, created_at, updated_at
                ) VALUES (
                    'repo-a', 'Repo A', 'http://mem0:8000',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO events (
                    id, project_id, operation, status, created_at
                ) VALUES (
                    'event-legacy', 'repo-a', 'memory.search', 'SUCCEEDED',
                    CURRENT_TIMESTAMP
                )
                """
            )
        )

    command.upgrade(config, "head")

    inspector = sa.inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns("events")}
    assert {
        "app_id",
        "user_id",
        "agent_id",
        "run_id",
        "correlation_id",
        "latency_ms",
        "result_count",
        "has_results",
    } <= set(columns)
    assert columns["app_id"]["nullable"] is True
    assert columns["result_count"]["nullable"] is False
    assert columns["has_results"]["nullable"] is False
    assert str(columns["result_count"]["default"]).strip("'()") == "0"
    assert str(columns["has_results"]["default"]).strip("'()") == "0"
    upgraded_indexes = {
        index["name"]: tuple(index["column_names"])
        for index in inspector.get_indexes("events")
    }
    assert REQUEST_TRACE_INDEXES <= set(upgraded_indexes)
    assert {
        name: upgraded_indexes[name] for name in REQUEST_TRACE_INDEX_COLUMNS
    } == REQUEST_TRACE_INDEX_COLUMNS

    with engine.connect() as connection:
        legacy = connection.execute(
            sa.text(
                """
                SELECT app_id, user_id, agent_id, run_id,
                       correlation_id, latency_ms, result_count, has_results
                FROM events
                WHERE id = 'event-legacy'
                """
            )
        ).mappings().one()
    assert legacy == {
        "app_id": None,
        "user_id": None,
        "agent_id": None,
        "run_id": None,
        "correlation_id": None,
        "latency_ms": None,
        "result_count": 0,
        "has_results": 0,
    }

    command.downgrade(config, "0004_memory_explorer_indexes")

    downgraded = sa.inspect(engine)
    assert {
        "app_id",
        "user_id",
        "agent_id",
        "run_id",
        "correlation_id",
        "latency_ms",
        "result_count",
        "has_results",
    }.isdisjoint(column["name"] for column in downgraded.get_columns("events"))
    assert REQUEST_TRACE_INDEXES.isdisjoint(
        index["name"] for index in downgraded.get_indexes("events")
    )


def test_request_trace_migration_has_exact_revision_chain() -> None:
    migration = importlib.import_module(
        "migrations.versions.0005_request_trace_fields"
    )

    assert migration.revision == "0005_request_trace_fields"
    assert migration.down_revision == "0004_memory_explorer_indexes"


def test_request_trace_migration_uses_postgres_bigint_for_result_count(
    monkeypatch,
) -> None:
    migration = importlib.import_module(
        "migrations.versions.0005_request_trace_fields"
    )
    added_columns: list[sa.Column] = []

    monkeypatch.setattr(
        migration,
        "op",
        SimpleNamespace(
            add_column=lambda table_name, column: added_columns.append(column),
            create_index=lambda *args, **kwargs: None,
        ),
    )

    migration.upgrade()

    result_count = next(
        column for column in added_columns if column.name == "result_count"
    )
    canonical_columns = {
        column.name: column
        for column in added_columns
        if column.name in {"app_id", "user_id", "agent_id", "run_id"}
    }
    assert isinstance(result_count.type, sa.BigInteger)
    assert result_count.type.compile(dialect=postgresql.dialect()) == "BIGINT"
    assert set(canonical_columns) == {"app_id", "user_id", "agent_id", "run_id"}
    assert all(column.nullable is True for column in canonical_columns.values())
    assert {
        column.type.compile(dialect=postgresql.dialect())
        for column in canonical_columns.values()
    } == {"VARCHAR(256)"}


def test_request_trace_migration_drops_entity_indexes_before_entity_columns(
    monkeypatch,
) -> None:
    migration = importlib.import_module(
        "migrations.versions.0005_request_trace_fields"
    )
    operations: list[tuple[str, str]] = []
    monkeypatch.setattr(
        migration,
        "op",
        SimpleNamespace(
            drop_index=lambda name, table_name: operations.append(("index", name)),
            drop_column=lambda table_name, name: operations.append(("column", name)),
        ),
    )

    migration.downgrade()

    for field_name in ("app", "user", "agent", "run"):
        assert operations.index(
            (
                "index",
                (
                    "ix_events_project_app_created"
                    if field_name == "app"
                    else f"ix_events_project_app_{field_name}_created"
                ),
            )
        ) < operations.index(("column", f"{field_name}_id"))


def test_entity_projection_migration_backfills_deduplicates_and_downgrades(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'entity-projections.sqlite3'}"
    config = _alembic_config(database_url)
    command.upgrade(config, "0005_request_trace_fields")
    engine = sa.create_engine(database_url, future=True)

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO projects (
                    id, name, default_app_id, mem0_base_url,
                    created_at, updated_at
                ) VALUES
                    (
                        'with-default', 'With Default', 'dashboard-app',
                        'http://mem0:8000', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    ),
                    (
                        'fallback-project', 'Fallback Project', NULL,
                        'http://mem0:8000', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                """
            )
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO entities (
                    id, project_id, entity_type, entity_id, display_name,
                    metadata_json, memory_count, last_seen_at,
                    created_at, updated_at
                ) VALUES
                    (
                        'duplicate-old', 'with-default', 'user', 'alice',
                        'Old Alice', '{}', 1, '2026-07-12 00:00:00',
                        '2026-07-12 00:00:00', '2026-07-12 00:00:00'
                    ),
                    (
                        'duplicate-new', 'with-default', 'user', 'alice',
                        'New Alice', '{}', 2, '2026-07-13 00:00:00',
                        '2026-07-12 00:00:00', '2026-07-13 00:00:00'
                    ),
                    (
                        'fallback-row', 'fallback-project', 'run', 'run-1',
                        NULL, '{}', 1, '2026-07-13 00:00:00',
                        '2026-07-13 00:00:00', '2026-07-13 00:00:00'
                    )
                """
            )
        )

    command.upgrade(config, "head")

    inspector = sa.inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns("entities")}
    assert columns["app_id"]["nullable"] is False
    constraints = {
        constraint["name"]: tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("entities")
    }
    assert constraints["uq_entities_project_app_type_id"] == (
        "project_id",
        "app_id",
        "entity_type",
        "entity_id",
    )
    indexes = {
        index["name"]: tuple(index["column_names"])
        for index in inspector.get_indexes("entities")
    }
    assert {
        name: indexes[name] for name in ENTITY_PROJECTION_INDEX_COLUMNS
    } == ENTITY_PROJECTION_INDEX_COLUMNS

    with engine.connect() as connection:
        rows = (
            connection.execute(
                sa.text(
                    """
                SELECT id, project_id, app_id, display_name
                FROM entities
                ORDER BY id
                """
                )
            )
            .mappings()
            .all()
        )
    assert rows == [
        {
            "id": "duplicate-new",
            "project_id": "with-default",
            "app_id": "dashboard-app",
            "display_name": "New Alice",
        },
        {
            "id": "fallback-row",
            "project_id": "fallback-project",
            "app_id": "fallback-project",
            "display_name": None,
        },
    ]

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO entities (
                    id, project_id, app_id, entity_type, entity_id,
                    metadata_json, memory_count, created_at, updated_at
                ) VALUES (
                    'other-app-same-id', 'with-default', 'other-app',
                    'user', 'alice', '{}', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO entities (
                    id, project_id, app_id, entity_type, entity_id,
                    metadata_json, memory_count, created_at, updated_at
                ) VALUES (
                    'same-app-duplicate', 'with-default', 'dashboard-app',
                    'user', 'alice', '{}', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )

    command.downgrade(config, "0005_request_trace_fields")

    downgraded = sa.inspect(engine)
    assert "app_id" not in {
        column["name"] for column in downgraded.get_columns("entities")
    }
    assert "uq_entities_project_app_type_id" not in {
        constraint["name"]
        for constraint in downgraded.get_unique_constraints("entities")
    }
    assert set(ENTITY_PROJECTION_INDEX_COLUMNS).isdisjoint(
        index["name"] for index in downgraded.get_indexes("entities")
    )
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO entities (
                    id, project_id, entity_type, entity_id,
                    metadata_json, memory_count, created_at, updated_at
                ) VALUES (
                    'legacy-after-downgrade', 'with-default', 'user', 'alice',
                    '{}', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )


def test_entity_projection_migration_has_exact_revision_chain() -> None:
    migration = importlib.import_module(
        "migrations.versions.0006_entity_projection_scope"
    )

    assert migration.revision == "0006_entity_projection_scope"
    assert migration.down_revision == "0005_request_trace_fields"


def test_phase2_head_rows_survive_0004_downgrade_and_reupgrade_exactly(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'phase2-lossless-roundtrip.sqlite3'}"
    config = _alembic_config(database_url)
    command.upgrade(config, "head")
    engine = sa.create_engine(database_url, future=True)

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO projects (
                    id, name, default_app_id, mem0_base_url, created_at, updated_at
                ) VALUES (
                    'repo-a', 'Repo A', 'app-a', 'http://mem0:8000',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO events (
                    id, project_id, app_id, user_id, agent_id, run_id,
                    operation, status, request_json, response_json, error_json,
                    correlation_id, latency_ms, result_count, has_results,
                    created_at
                ) VALUES (
                    'head-event', 'repo-a', 'app-b', 'alice', 'agent-b', 'run-b',
                    'memory.list', 'SUCCEEDED', '{}', '{}', '{}',
                    'correlation-b', 12.5, 7, 1, CURRENT_TIMESTAMP
                )
                """
            )
        )
        for row_id, app_id, display_name, memory_count in (
            ("head-entity-a", "app-a", "Alice A", 2),
            ("head-entity-b", "app-b", "Alice B", 3),
        ):
            connection.execute(
                sa.text(
                    """
                    INSERT INTO entities (
                        id, project_id, app_id, entity_type, entity_id,
                        display_name, metadata_json, memory_count,
                        created_at, updated_at
                    ) VALUES (
                        :id, 'repo-a', :app_id, 'user', 'alice',
                        :display_name, '{}', :memory_count,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "id": row_id,
                    "app_id": app_id,
                    "display_name": display_name,
                    "memory_count": memory_count,
                },
            )

    command.downgrade(config, "0004_memory_explorer_indexes")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO events (
                    id, project_id, operation, status, request_json,
                    response_json, error_json, created_at
                ) VALUES (
                    'legacy-event-after-downgrade', 'repo-a', 'memory.search',
                    'SUCCEEDED', '{}', '{}', '{}', CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO entities (
                    id, project_id, entity_type, entity_id, metadata_json,
                    memory_count, created_at, updated_at
                ) VALUES (
                    'legacy-entity-after-downgrade', 'repo-a', 'run', 'run-legacy',
                    '{}', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )

    command.upgrade(config, "head")
    with engine.connect() as connection:
        event = connection.execute(
            sa.text(
                """
                SELECT app_id, user_id, agent_id, run_id, correlation_id,
                       latency_ms, result_count, has_results
                FROM events WHERE id = 'head-event'
                """
            )
        ).mappings().one()
        legacy_event = connection.execute(
            sa.text(
                """
                SELECT app_id, correlation_id, latency_ms, result_count, has_results
                FROM events WHERE id = 'legacy-event-after-downgrade'
                """
            )
        ).mappings().one()
        entities = connection.execute(
            sa.text(
                """
                SELECT id, app_id, display_name, memory_count
                FROM entities ORDER BY id
                """
            )
        ).mappings().all()
        tables = set(sa.inspect(connection).get_table_names())

    assert event == {
        "app_id": "app-b",
        "user_id": "alice",
        "agent_id": "agent-b",
        "run_id": "run-b",
        "correlation_id": "correlation-b",
        "latency_ms": 12.5,
        "result_count": 7,
        "has_results": 1,
    }
    assert legacy_event == {
        "app_id": None,
        "correlation_id": None,
        "latency_ms": None,
        "result_count": 0,
        "has_results": 0,
    }
    assert entities == [
        {
            "id": "head-entity-a",
            "app_id": "app-a",
            "display_name": "Alice A",
            "memory_count": 2,
        },
        {
            "id": "head-entity-b",
            "app_id": "app-b",
            "display_name": "Alice B",
            "memory_count": 3,
        },
        {
            "id": "legacy-entity-after-downgrade",
            "app_id": "app-a",
            "display_name": None,
            "memory_count": 1,
        },
    ]
    assert "_compat_0005_request_trace_fields" not in tables
    assert "_compat_0006_entity_projection_scope" not in tables


def test_request_trace_downgrade_rebuilds_interrupted_empty_snapshot(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'trace-interruption.sqlite3'}"
    config = _alembic_config(database_url)
    command.upgrade(config, "0005_request_trace_fields")
    engine = sa.create_engine(database_url, future=True)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO projects (
                    id, name, default_app_id, mem0_base_url, created_at, updated_at
                ) VALUES (
                    'repo-a', 'Repo A', 'app-a', 'http://mem0:8000',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO events (
                    id, project_id, app_id, user_id, agent_id, run_id,
                    operation, status, request_json, response_json, error_json,
                    correlation_id, latency_ms, result_count, has_results, created_at
                ) VALUES (
                    'trace-event', 'repo-a', 'app-b', 'alice', 'agent-b', 'run-b',
                    'memory.list', 'SUCCEEDED', '{}', '{}', '{}',
                    'correlation-b', 9.5, 4, 1, CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            sa.text(
                """
                CREATE TABLE _compat_0005_request_trace_fields (
                    event_id VARCHAR(36) PRIMARY KEY,
                    app_id VARCHAR(256), user_id VARCHAR(256),
                    agent_id VARCHAR(256), run_id VARCHAR(256),
                    correlation_id VARCHAR(256), latency_ms FLOAT,
                    result_count BIGINT NOT NULL, has_results INTEGER NOT NULL
                )
                """
            )
        )

    command.downgrade(config, "0004_memory_explorer_indexes")
    command.upgrade(config, "0005_request_trace_fields")
    with engine.connect() as connection:
        restored = connection.execute(
            sa.text(
                """
                SELECT app_id, user_id, agent_id, run_id, correlation_id,
                       latency_ms, result_count, has_results
                FROM events WHERE id = 'trace-event'
                """
            )
        ).mappings().one()

    assert restored == {
        "app_id": "app-b",
        "user_id": "alice",
        "agent_id": "agent-b",
        "run_id": "run-b",
        "correlation_id": "correlation-b",
        "latency_ms": 9.5,
        "result_count": 4,
        "has_results": 1,
    }


def test_entity_projection_downgrade_rebuilds_interrupted_empty_snapshot(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'entity-interruption.sqlite3'}"
    config = _alembic_config(database_url)
    command.upgrade(config, "0006_entity_projection_scope")
    engine = sa.create_engine(database_url, future=True)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO projects (
                    id, name, default_app_id, mem0_base_url, created_at, updated_at
                ) VALUES (
                    'repo-a', 'Repo A', 'app-a', 'http://mem0:8000',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO entities (
                    id, project_id, app_id, entity_type, entity_id,
                    metadata_json, memory_count, created_at, updated_at
                ) VALUES (
                    'entity-b', 'repo-a', 'app-b', 'user', 'alice',
                    '{}', 2, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            sa.text(
                """
                CREATE TABLE _compat_0006_entity_projection_scope (
                    entity_id VARCHAR(36) PRIMARY KEY,
                    app_id VARCHAR(256) NOT NULL
                )
                """
            )
        )

    command.downgrade(config, "0005_request_trace_fields")
    command.upgrade(config, "0006_entity_projection_scope")
    with engine.connect() as connection:
        restored_app_id = connection.scalar(
            sa.text("SELECT app_id FROM entities WHERE id = 'entity-b'")
        )

    assert restored_app_id == "app-b"


@pytest.mark.parametrize(
    "blocking_status",
    ["ACTIVE", "UNKNOWN", "PENDING", "EXHAUSTED"],
)
def test_mutation_intent_downgrade_refuses_unresolved_rows_before_drop(
    tmp_path,
    blocking_status: str,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'intent-downgrade.sqlite3'}"
    config = _alembic_config(database_url)
    command.upgrade(config, "head")
    engine = sa.create_engine(database_url, future=True)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO projects (
                    id, name, default_app_id, mem0_base_url, created_at, updated_at
                ) VALUES (
                    'repo-a', 'Repo A', 'app-a', 'http://mem0:8000',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO events (
                    id, project_id, app_id, operation, status,
                    request_json, response_json, error_json, created_at
                ) VALUES (
                    'intent-event', 'repo-a', 'app-a', 'memory.delete', 'FAILED',
                    '{}', '{}', '{}', CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO mutation_intents (
                    id, project_id, app_id, event_id, operation, operation_key,
                    status, payload_json, result_json, error_json, attempt_count,
                    created_at, updated_at
                ) VALUES (
                    'intent-one', 'repo-a', 'app-a', 'intent-event',
                    'memory.delete', 'operation-key', :status, '{}', '{}', '{}', 2,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            ),
            {"status": blocking_status},
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO mutation_intent_targets (
                    id, intent_id, memory_id, ordinal, status, error_json,
                    created_at, updated_at
                ) VALUES (
                    'target-one', 'intent-one', 'memory-one', 0, 'PENDING', '{}',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )

    with pytest.raises(RuntimeError, match="nonterminal mutation intents"):
        command.downgrade(config, "0006_entity_projection_scope")

    with engine.connect() as connection:
        tables = set(sa.inspect(connection).get_table_names())
        intent_count = connection.scalar(
            sa.text("SELECT COUNT(*) FROM mutation_intents")
        )
        target_count = connection.scalar(
            sa.text("SELECT COUNT(*) FROM mutation_intent_targets")
        )
        persisted_status = connection.scalar(
            sa.text("SELECT status FROM mutation_intents WHERE id = 'intent-one'")
        )
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert {"mutation_intents", "mutation_intent_targets"}.issubset(tables)
    assert intent_count == 1
    assert target_count == 1
    assert persisted_status == blocking_status
    assert revision == "0007_mutation_intents"

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                UPDATE mutation_intents
                SET status = 'FAILED', completed_at = CURRENT_TIMESTAMP
                WHERE id = 'intent-one'
                """
            )
        )
    command.downgrade(config, "0006_entity_projection_scope")
    command.upgrade(config, "head")


def test_terminal_mutation_intent_history_allows_downgrade(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'terminal-intents.sqlite3'}"
    config = _alembic_config(database_url)
    command.upgrade(config, "head")
    engine = sa.create_engine(database_url, future=True)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO projects (
                    id, name, default_app_id, mem0_base_url, created_at, updated_at
                ) VALUES (
                    'repo-a', 'Repo A', 'app-a', 'http://mem0:8000',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        for ordinal, status in enumerate(("COMPLETED", "FAILED", "PARTIAL")):
            connection.execute(
                sa.text(
                    """
                    INSERT INTO events (
                        id, project_id, app_id, operation, status,
                        request_json, response_json, error_json, created_at
                    ) VALUES (
                        :event_id, 'repo-a', 'app-a', 'memory.delete', 'FAILED',
                        '{}', '{}', '{}', CURRENT_TIMESTAMP
                    )
                    """
                ),
                {"event_id": f"terminal-event-{ordinal}"},
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO mutation_intents (
                        id, project_id, app_id, event_id, operation, operation_key,
                        status, payload_json, result_json, error_json, attempt_count,
                        created_at, updated_at, completed_at
                    ) VALUES (
                        :intent_id, 'repo-a', 'app-a', :event_id,
                        'memory.delete', :operation_key, :status, '{}', '{}', '{}', 1,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "intent_id": f"terminal-intent-{ordinal}",
                    "event_id": f"terminal-event-{ordinal}",
                    "operation_key": f"terminal-key-{ordinal}",
                    "status": status,
                },
            )

    command.downgrade(config, "0006_entity_projection_scope")
    assert "mutation_intents" not in sa.inspect(engine).get_table_names()


def test_request_trace_upgrade_rejects_snapshot_without_ready_marker(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'trace-invalid-snapshot.sqlite3'}"
    config = _alembic_config(database_url)
    command.upgrade(config, "0005_request_trace_fields")
    engine = sa.create_engine(database_url, future=True)
    command.downgrade(config, "0004_memory_explorer_indexes")
    with engine.begin() as connection:
        connection.execute(sa.text("DROP TABLE _compat_0005_request_trace_fields"))
        connection.execute(
            sa.text(
                """
                CREATE TABLE _compat_0005_request_trace_fields (
                    event_id VARCHAR(36), app_id VARCHAR(256),
                    snapshot_kind VARCHAR(16), snapshot_row_count BIGINT
                )
                """
            )
        )

    with pytest.raises(RuntimeError, match="invalid 0005 compatibility snapshot"):
        command.upgrade(config, "0005_request_trace_fields")

    inspector = sa.inspect(engine)
    assert "_compat_0005_request_trace_fields" in inspector.get_table_names()
    assert "app_id" not in {
        column["name"] for column in inspector.get_columns("events")
    }


def test_entity_projection_upgrade_rejects_snapshot_without_ready_marker(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'entity-invalid-snapshot.sqlite3'}"
    config = _alembic_config(database_url)
    command.upgrade(config, "0006_entity_projection_scope")
    engine = sa.create_engine(database_url, future=True)
    command.downgrade(config, "0005_request_trace_fields")
    with engine.begin() as connection:
        connection.execute(sa.text("DROP TABLE _compat_0006_entity_projection_scope"))
        connection.execute(
            sa.text(
                """
                CREATE TABLE _compat_0006_entity_projection_scope (
                    entity_id VARCHAR(36), app_id VARCHAR(256),
                    snapshot_kind VARCHAR(16), snapshot_row_count BIGINT
                )
                """
            )
        )

    with pytest.raises(RuntimeError, match="invalid 0006 compatibility snapshot"):
        command.upgrade(config, "0006_entity_projection_scope")

    inspector = sa.inspect(engine)
    assert "_compat_0006_entity_projection_scope" in inspector.get_table_names()
    assert "app_id" not in {
        column["name"] for column in inspector.get_columns("entities")
    }
