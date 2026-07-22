import importlib
import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
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

CONSOLIDATION_TABLES = {
    "consolidation_policies",
    "consolidation_runs",
    "consolidation_proposals",
    "consolidation_lineage",
}

CONSOLIDATION_MEMORY_COLUMNS = {
    "content_hash",
    "content_length",
    "normalized_type",
    "source",
    "pinned",
    "expires_at",
    "last_observed_at",
    "consolidation_state",
    "shadowed_by_proposal_id",
}


def test_consolidation_migration_upgrades_from_mutation_intents(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'consolidation.sqlite3'}"
    config = _alembic_config(database_url)
    command.upgrade(config, "0007_mutation_intents")
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
                INSERT INTO memories_index (
                    id, project_id, mem0_memory_id, entity_refs_json,
                    metadata_projection_json, created_at, updated_at
                ) VALUES (
                    'index-1', 'repo-a', 'mem-1', '[]', '{}',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )

    command.upgrade(config, "head")

    inspector = sa.inspect(engine)
    assert CONSOLIDATION_TABLES.issubset(inspector.get_table_names())
    columns = {
        column["name"]: column
        for column in inspector.get_columns("memories_index")
    }
    assert CONSOLIDATION_MEMORY_COLUMNS.issubset(columns)
    with engine.connect() as connection:
        row = connection.execute(
            sa.text(
                """
                SELECT pinned, consolidation_state
                FROM memories_index WHERE id = 'index-1'
                """
            )
        ).mappings().one()
        revision = connection.scalar(
            sa.text("SELECT version_num FROM alembic_version")
        )
    assert row == {"pinned": 0, "consolidation_state": "ACTIVE"}
    assert revision == "0008_memory_consolidation"

    index_names = {
        index["name"]
        for table in (
            "memories_index",
            "consolidation_runs",
            "consolidation_proposals",
            "consolidation_lineage",
        )
        for index in inspector.get_indexes(table)
    }
    assert {
        "ix_memories_index_consolidation_exact",
        "ix_memories_index_consolidation_dirty",
        "ix_consolidation_runs_scope_status_created",
        "ix_consolidation_proposals_scope_status",
        "ix_consolidation_lineage_scope_applied",
    }.issubset(index_names)


@pytest.mark.parametrize(
    ("table", "status"),
    [
        ("consolidation_runs", "PENDING"),
        ("consolidation_runs", "RUNNING"),
        ("consolidation_proposals", "APPROVED"),
        ("consolidation_proposals", "SHADOWED"),
    ],
)
def test_consolidation_downgrade_refuses_nonterminal_work(
    tmp_path, table: str, status: str
) -> None:
    database_url = f"sqlite:///{tmp_path / f'consolidation-{table}-{status}.sqlite3'}"
    config = _alembic_config(database_url)
    command.upgrade(config, "head")
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
                INSERT INTO consolidation_runs (
                    id, project_id, app_id, mode, status, counts_json,
                    error_json, created_at, updated_at
                ) VALUES (
                    'run-1', 'repo-a', 'app-a', 'OBSERVE',
                    :run_status, '{}', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            ),
            {"run_status": status if table == "consolidation_runs" else "SUCCEEDED"},
        )
        if table == "consolidation_proposals":
            connection.execute(
                sa.text(
                    """
                    INSERT INTO consolidation_proposals (
                        id, run_id, project_id, app_id, proposal_key, kind,
                        status, source_ids_json, evidence_json, created_at,
                        updated_at
                    ) VALUES (
                        'proposal-1', 'run-1', 'repo-a', 'app-a', 'key-1',
                        'EXACT_DUPLICATE', :status, '["mem-1","mem-2"]',
                        '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {"status": status},
            )

    with pytest.raises(RuntimeError, match="nonterminal consolidation"):
        command.downgrade(config, "0007_mutation_intents")

    assert CONSOLIDATION_TABLES.issubset(sa.inspect(engine).get_table_names())


def _alembic_config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.set_main_option("path_separator", "os")
    return config


def test_alembic_logging_keeps_existing_application_loggers_enabled(
    tmp_path,
) -> None:
    logger = logging.getLogger("mem0_sidecar.direct_write_sync")
    logger.disabled = False
    config = _alembic_config(f"sqlite:///{tmp_path / 'alembic.sqlite3'}")

    command.upgrade(config, "head")

    assert logger.disabled is False


class _ThreadLocalMigrationOp:
    def __init__(self) -> None:
        self._local = threading.local()

    def set_bind(self, bind) -> None:
        self._local.bind = bind

    def get_bind(self):
        return self._local.bind

    def drop_index(self, name: str, *, table_name: str) -> None:
        del table_name
        self.get_bind().exec_driver_sql(f'DROP INDEX "{name}"')

    def drop_table(self, name: str) -> None:
        self.get_bind().exec_driver_sql(f'DROP TABLE "{name}"')


class _GuardSignalingBind:
    def __init__(
        self,
        connection,
        *,
        guard_counted: threading.Event,
        release_guard: threading.Event | None = None,
    ) -> None:
        self._connection = connection
        self.dialect = connection.dialect
        self.guard_counted = guard_counted
        self.release_guard = release_guard

    def scalar(self, statement, *args, **kwargs):
        result = self._connection.scalar(statement, *args, **kwargs)
        if "FROM mutation_intents" in str(statement):
            self.guard_counted.set()
            if self.release_guard is not None:
                assert self.release_guard.wait(5)
        return result

    def execute(self, statement, *args, **kwargs):
        return self._connection.execute(statement, *args, **kwargs)

    def exec_driver_sql(self, statement, *args, **kwargs):
        return self._connection.exec_driver_sql(statement, *args, **kwargs)


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


def _replace_0005_snapshot_with_exact_b502a26_schema(
    connection,
    *,
    row: dict[str, object] | None,
) -> None:
    connection.execute(sa.text("DROP TABLE _compat_0005_request_trace_fields"))
    sa.Table(
        "_compat_0005_request_trace_fields",
        sa.MetaData(),
        sa.Column("event_id", sa.String(length=36), primary_key=True),
        sa.Column("app_id", sa.String(length=256), nullable=True),
        sa.Column("user_id", sa.String(length=256), nullable=True),
        sa.Column("agent_id", sa.String(length=256), nullable=True),
        sa.Column("run_id", sa.String(length=256), nullable=True),
        sa.Column("correlation_id", sa.String(length=256), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("result_count", sa.BigInteger(), nullable=False),
        sa.Column("has_results", sa.Integer(), nullable=False),
    ).create(connection)
    inspector = sa.inspect(connection)
    assert [
        (
            column["name"],
            type(column["type"]).__name__,
            getattr(column["type"], "length", None),
            column["nullable"],
            column.get("default"),
        )
        for column in inspector.get_columns(
            "_compat_0005_request_trace_fields"
        )
    ] == [
        ("event_id", "VARCHAR", 36, False, None),
        ("app_id", "VARCHAR", 256, True, None),
        ("user_id", "VARCHAR", 256, True, None),
        ("agent_id", "VARCHAR", 256, True, None),
        ("run_id", "VARCHAR", 256, True, None),
        ("correlation_id", "VARCHAR", 256, True, None),
        ("latency_ms", "FLOAT", None, True, None),
        ("result_count", "BIGINT", None, False, None),
        ("has_results", "INTEGER", None, False, None),
    ]
    if row is not None:
        connection.execute(
            sa.text(
                """
                INSERT INTO _compat_0005_request_trace_fields (
                    event_id, app_id, user_id, agent_id, run_id,
                    correlation_id, latency_ms, result_count, has_results
                ) VALUES (
                    :event_id, :app_id, :user_id, :agent_id, :run_id,
                    :correlation_id, :latency_ms, :result_count, :has_results
                )
                """
            ),
            row,
        )
    assert inspector.get_pk_constraint(
        "_compat_0005_request_trace_fields"
    )["constrained_columns"] == ["event_id"]
    assert inspector.get_indexes("_compat_0005_request_trace_fields") == []
    assert inspector.get_unique_constraints(
        "_compat_0005_request_trace_fields"
    ) == []
    assert inspector.get_foreign_keys("_compat_0005_request_trace_fields") == []
    assert inspector.get_check_constraints(
        "_compat_0005_request_trace_fields"
    ) == []


def _replace_0006_snapshot_with_exact_b502a26_schema(
    connection,
    *,
    row: dict[str, object] | None,
) -> None:
    connection.execute(sa.text("DROP TABLE _compat_0006_entity_projection_scope"))
    sa.Table(
        "_compat_0006_entity_projection_scope",
        sa.MetaData(),
        sa.Column("entity_id", sa.String(length=36), primary_key=True),
        sa.Column("app_id", sa.String(length=256), nullable=False),
    ).create(connection)
    inspector = sa.inspect(connection)
    assert [
        (
            column["name"],
            type(column["type"]).__name__,
            getattr(column["type"], "length", None),
            column["nullable"],
            column.get("default"),
        )
        for column in inspector.get_columns(
            "_compat_0006_entity_projection_scope"
        )
    ] == [
        ("entity_id", "VARCHAR", 36, False, None),
        ("app_id", "VARCHAR", 256, False, None),
    ]
    if row is not None:
        connection.execute(
            sa.text(
                """
                INSERT INTO _compat_0006_entity_projection_scope (
                    entity_id, app_id
                ) VALUES (:entity_id, :app_id)
                """
            ),
            row,
        )
    assert inspector.get_pk_constraint(
        "_compat_0006_entity_projection_scope"
    )["constrained_columns"] == ["entity_id"]
    assert inspector.get_indexes("_compat_0006_entity_projection_scope") == []
    assert inspector.get_unique_constraints(
        "_compat_0006_entity_projection_scope"
    ) == []
    assert inspector.get_foreign_keys("_compat_0006_entity_projection_scope") == []
    assert inspector.get_check_constraints(
        "_compat_0006_entity_projection_scope"
    ) == []


def test_request_trace_upgrade_restores_exact_b502a26_legacy_snapshot(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'trace-b502a26.sqlite3'}"
    config = _alembic_config(database_url)
    command.upgrade(config, "0005_request_trace_fields")
    engine = sa.create_engine(database_url, future=True)
    legacy_row = {
        "event_id": "legacy-trace",
        "app_id": "app-b",
        "user_id": "alice",
        "agent_id": "agent-b",
        "run_id": "run-b",
        "correlation_id": "correlation-b",
        "latency_ms": 17.5,
        "result_count": 6,
        "has_results": 1,
    }
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
                    'legacy-trace', 'repo-a', 'wrong', 'wrong', 'wrong', 'wrong',
                    'memory.list', 'SUCCEEDED', '{}', '{}', '{}',
                    'wrong', 1.0, 1, 0, CURRENT_TIMESTAMP
                )
                """
            )
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
                    'downgraded-era-trace', 'repo-a', 'memory.search',
                    'SUCCEEDED', '{}', '{}', '{}', CURRENT_TIMESTAMP
                )
                """
            )
        )
        _replace_0005_snapshot_with_exact_b502a26_schema(
            connection,
            row=legacy_row,
        )

    command.upgrade(config, "0005_request_trace_fields")
    with engine.connect() as connection:
        restored = connection.execute(
            sa.text(
                """
                SELECT app_id, user_id, agent_id, run_id, correlation_id,
                       latency_ms, result_count, has_results
                FROM events WHERE id = 'legacy-trace'
                """
            )
        ).mappings().one()
        downgraded_era = connection.execute(
            sa.text(
                """
                SELECT app_id, user_id, agent_id, run_id, correlation_id,
                       latency_ms, result_count, has_results
                FROM events WHERE id = 'downgraded-era-trace'
                """
            )
        ).mappings().one()
    assert restored == {
        key: value for key, value in legacy_row.items() if key != "event_id"
    }
    assert downgraded_era == {
        "app_id": None,
        "user_id": None,
        "agent_id": None,
        "run_id": None,
        "correlation_id": None,
        "latency_ms": None,
        "result_count": 0,
        "has_results": 0,
    }


def test_entity_projection_upgrade_restores_exact_b502a26_legacy_snapshot(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'entity-b502a26.sqlite3'}"
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
                    'legacy-entity', 'repo-a', 'wrong', 'user', 'alice',
                    '{}', 2, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )

    command.downgrade(config, "0005_request_trace_fields")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO entities (
                    id, project_id, entity_type, entity_id,
                    metadata_json, memory_count, created_at, updated_at
                ) VALUES (
                    'downgraded-era-entity', 'repo-a', 'run', 'run-new',
                    '{}', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        _replace_0006_snapshot_with_exact_b502a26_schema(
            connection,
            row={"entity_id": "legacy-entity", "app_id": "app-b"},
        )

    command.upgrade(config, "0006_entity_projection_scope")
    with engine.connect() as connection:
        assert connection.scalar(
            sa.text("SELECT app_id FROM entities WHERE id = 'legacy-entity'")
        ) == "app-b"
        assert connection.scalar(
            sa.text("SELECT app_id FROM entities WHERE id = 'downgraded-era-entity'")
        ) == "app-a"


@pytest.mark.parametrize("revision", ["0005", "0006"])
def test_exact_b502a26_empty_legacy_snapshot_upgrades_only_empty_source(
    tmp_path,
    revision: str,
) -> None:
    database_url = f"sqlite:///{tmp_path / f'empty-legacy-{revision}.sqlite3'}"
    config = _alembic_config(database_url)
    target = (
        "0005_request_trace_fields"
        if revision == "0005"
        else "0006_entity_projection_scope"
    )
    prior = (
        "0004_memory_explorer_indexes"
        if revision == "0005"
        else "0005_request_trace_fields"
    )
    command.upgrade(config, target)
    engine = sa.create_engine(database_url, future=True)
    command.downgrade(config, prior)
    with engine.begin() as connection:
        if revision == "0005":
            _replace_0005_snapshot_with_exact_b502a26_schema(connection, row=None)
        else:
            _replace_0006_snapshot_with_exact_b502a26_schema(connection, row=None)

    command.upgrade(config, target)
    source_table = "events" if revision == "0005" else "entities"
    with engine.connect() as connection:
        assert connection.scalar(sa.text(f"SELECT COUNT(*) FROM {source_table}")) == 0


@pytest.mark.parametrize("revision", ["0005", "0006"])
def test_exact_b502a26_empty_legacy_snapshot_rejects_nonempty_source(
    tmp_path,
    revision: str,
) -> None:
    database_url = f"sqlite:///{tmp_path / f'ambiguous-legacy-{revision}.sqlite3'}"
    config = _alembic_config(database_url)
    target = (
        "0005_request_trace_fields"
        if revision == "0005"
        else "0006_entity_projection_scope"
    )
    prior = (
        "0004_memory_explorer_indexes"
        if revision == "0005"
        else "0005_request_trace_fields"
    )
    command.upgrade(config, target)
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
        if revision == "0005":
            connection.execute(
                sa.text(
                    """
                    INSERT INTO events (
                        id, project_id, operation, status, request_json,
                        response_json, error_json, created_at
                    ) VALUES (
                        'ambiguous-event', 'repo-a', 'memory.list', 'SUCCEEDED',
                        '{}', '{}', '{}', CURRENT_TIMESTAMP
                    )
                    """
                )
            )
        else:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO entities (
                        id, project_id, app_id, entity_type, entity_id,
                        metadata_json, memory_count, created_at, updated_at
                    ) VALUES (
                        'ambiguous-entity', 'repo-a', 'app-a', 'user', 'alice',
                        '{}', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
            )
    command.downgrade(config, prior)
    with engine.begin() as connection:
        if revision == "0005":
            _replace_0005_snapshot_with_exact_b502a26_schema(connection, row=None)
        else:
            _replace_0006_snapshot_with_exact_b502a26_schema(connection, row=None)

    with pytest.raises(
        RuntimeError,
        match=rf"ambiguous empty {revision} legacy compatibility snapshot",
    ):
        command.upgrade(config, target)


@pytest.mark.parametrize("revision", ["0005", "0006"])
def test_exact_b502a26_legacy_snapshot_rejects_deleted_source_reference(
    tmp_path,
    revision: str,
) -> None:
    database_url = f"sqlite:///{tmp_path / f'orphan-legacy-{revision}.sqlite3'}"
    config = _alembic_config(database_url)
    target = (
        "0005_request_trace_fields"
        if revision == "0005"
        else "0006_entity_projection_scope"
    )
    prior = (
        "0004_memory_explorer_indexes"
        if revision == "0005"
        else "0005_request_trace_fields"
    )
    command.upgrade(config, target)
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
        if revision == "0005":
            connection.execute(
                sa.text(
                    """
                    INSERT INTO events (
                        id, project_id, operation, status, request_json,
                        response_json, error_json, created_at
                    ) VALUES (
                        'deleted-event', 'repo-a', 'memory.list', 'SUCCEEDED',
                        '{}', '{}', '{}', CURRENT_TIMESTAMP
                    )
                    """
                )
            )
        else:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO entities (
                        id, project_id, app_id, entity_type, entity_id,
                        metadata_json, memory_count, created_at, updated_at
                    ) VALUES (
                        'deleted-entity', 'repo-a', 'app-a', 'user', 'alice',
                        '{}', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
            )
    command.downgrade(config, prior)
    with engine.begin() as connection:
        if revision == "0005":
            connection.execute(sa.text("DELETE FROM events WHERE id = 'deleted-event'"))
            _replace_0005_snapshot_with_exact_b502a26_schema(
                connection,
                row={
                    "event_id": "deleted-event",
                    "app_id": "app-a",
                    "user_id": None,
                    "agent_id": None,
                    "run_id": None,
                    "correlation_id": None,
                    "latency_ms": None,
                    "result_count": 0,
                    "has_results": 0,
                },
            )
        else:
            connection.execute(
                sa.text("DELETE FROM entities WHERE id = 'deleted-entity'")
            )
            _replace_0006_snapshot_with_exact_b502a26_schema(
                connection,
                row={"entity_id": "deleted-entity", "app_id": "app-a"},
            )

    with pytest.raises(
        RuntimeError,
        match=rf"invalid {revision} legacy compatibility snapshot content",
    ):
        command.upgrade(config, target)


@pytest.mark.parametrize("revision", ["0005", "0006"])
def test_exact_legacy_validator_rejects_constraint_lookalike(
    tmp_path,
    monkeypatch,
    revision: str,
) -> None:
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / f'legacy-lookalike-{revision}.sqlite3'}",
        future=True,
    )
    migration = importlib.import_module(
        f"migrations.versions.{revision}_"
        + (
            "request_trace_fields"
            if revision == "0005"
            else "entity_projection_scope"
        )
    )
    with engine.begin() as connection:
        if revision == "0005":
            connection.execute(sa.text("CREATE TABLE events (id VARCHAR(36))"))
            connection.execute(sa.text("INSERT INTO events VALUES ('event-one')"))
            connection.execute(
                sa.text(
                    """
                    CREATE TABLE _compat_0005_request_trace_fields (
                        event_id VARCHAR(36),
                        app_id VARCHAR(256), user_id VARCHAR(256),
                        agent_id VARCHAR(256), run_id VARCHAR(256),
                        correlation_id VARCHAR(256), latency_ms FLOAT,
                        result_count BIGINT NOT NULL, has_results INTEGER NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO _compat_0005_request_trace_fields
                    VALUES ('event-one', NULL, NULL, NULL, NULL, NULL, NULL, 0, 0)
                    """
                )
            )
        else:
            connection.execute(sa.text("CREATE TABLE entities (id VARCHAR(36))"))
            connection.execute(sa.text("INSERT INTO entities VALUES ('entity-one')"))
            connection.execute(
                sa.text(
                    """
                    CREATE TABLE _compat_0006_entity_projection_scope (
                        entity_id VARCHAR(36) PRIMARY KEY,
                        app_id VARCHAR(256)
                    )
                    """
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO _compat_0006_entity_projection_scope
                    VALUES ('entity-one', 'app-a')
                    """
                )
            )
        monkeypatch.setattr(
            migration,
            "op",
            SimpleNamespace(get_bind=lambda: connection),
        )
        with pytest.raises(
            RuntimeError,
            match=rf"invalid {revision} compatibility snapshot structure",
        ):
            migration._validate_legacy_compatibility_snapshot()


@pytest.mark.parametrize("revision", ["0005", "0006"])
def test_exact_legacy_validator_rejects_dialect_type_and_nullability_lookalike(
    tmp_path,
    monkeypatch,
    revision: str,
) -> None:
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / f'legacy-type-lookalike-{revision}.sqlite3'}",
        future=True,
    )
    migration = importlib.import_module(
        f"migrations.versions.{revision}_"
        + (
            "request_trace_fields"
            if revision == "0005"
            else "entity_projection_scope"
        )
    )
    with engine.begin() as connection:
        if revision == "0005":
            connection.execute(sa.text("CREATE TABLE events (id VARCHAR(36))"))
            connection.execute(sa.text("INSERT INTO events VALUES ('event-one')"))
            connection.execute(
                sa.text(
                    """
                    CREATE TABLE _compat_0005_request_trace_fields (
                        event_id VARCHAR(36) PRIMARY KEY,
                        app_id VARCHAR(256) NOT NULL, user_id VARCHAR(256),
                        agent_id VARCHAR(256), run_id VARCHAR(256),
                        correlation_id VARCHAR(256), latency_ms REAL,
                        result_count BIGINT NOT NULL,
                        has_results SMALLINT NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO _compat_0005_request_trace_fields
                    VALUES ('event-one', 'app-a', NULL, NULL, NULL, NULL, 1.0, 0, 0)
                    """
                )
            )
        else:
            connection.execute(sa.text("CREATE TABLE entities (id VARCHAR(36))"))
            connection.execute(sa.text("INSERT INTO entities VALUES ('entity-one')"))
            connection.execute(
                sa.text(
                    """
                    CREATE TABLE _compat_0006_entity_projection_scope (
                        entity_id CHAR(36) PRIMARY KEY,
                        app_id NVARCHAR(256) NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO _compat_0006_entity_projection_scope
                    VALUES ('entity-one', 'app-a')
                    """
                )
            )
        monkeypatch.setattr(
            migration,
            "op",
            SimpleNamespace(get_bind=lambda: connection),
        )
        with pytest.raises(
            RuntimeError,
            match=rf"invalid {revision} compatibility snapshot structure",
        ):
            migration._validate_legacy_compatibility_snapshot()


@pytest.mark.parametrize("revision", ["0005", "0006"])
def test_exact_legacy_validator_rejects_reordered_columns(
    tmp_path,
    monkeypatch,
    revision: str,
) -> None:
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / f'legacy-reordered-{revision}.sqlite3'}",
        future=True,
    )
    migration = importlib.import_module(
        f"migrations.versions.{revision}_"
        + (
            "request_trace_fields"
            if revision == "0005"
            else "entity_projection_scope"
        )
    )
    with engine.begin() as connection:
        source_table = "events" if revision == "0005" else "entities"
        source_id = "event-one" if revision == "0005" else "entity-one"
        connection.execute(
            sa.text(f"CREATE TABLE {source_table} (id VARCHAR(36))")
        )
        connection.execute(
            sa.text(f"INSERT INTO {source_table} VALUES (:source_id)"),
            {"source_id": source_id},
        )
        if revision == "0005":
            connection.execute(
                sa.text(
                    """
                    CREATE TABLE _compat_0005_request_trace_fields (
                        app_id VARCHAR(256),
                        event_id VARCHAR(36) NOT NULL PRIMARY KEY,
                        user_id VARCHAR(256), agent_id VARCHAR(256),
                        run_id VARCHAR(256), correlation_id VARCHAR(256),
                        latency_ms FLOAT, result_count BIGINT NOT NULL,
                        has_results INTEGER NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO _compat_0005_request_trace_fields
                    VALUES (NULL, 'event-one', NULL, NULL, NULL, NULL, NULL, 0, 0)
                    """
                )
            )
        else:
            connection.execute(
                sa.text(
                    """
                    CREATE TABLE _compat_0006_entity_projection_scope (
                        app_id VARCHAR(256) NOT NULL,
                        entity_id VARCHAR(36) NOT NULL PRIMARY KEY
                    )
                    """
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO _compat_0006_entity_projection_scope
                    VALUES ('app-a', 'entity-one')
                    """
                )
            )
        monkeypatch.setattr(
            migration,
            "op",
            SimpleNamespace(get_bind=lambda: connection),
        )
        with pytest.raises(
            RuntimeError,
            match=rf"invalid {revision} compatibility snapshot structure",
        ):
            migration._validate_legacy_compatibility_snapshot()


@pytest.mark.parametrize("revision", ["0005", "0006"])
@pytest.mark.parametrize("schema_object", ["index", "unique", "foreign", "check"])
def test_exact_legacy_validator_rejects_unexpected_schema_objects(
    tmp_path,
    monkeypatch,
    revision: str,
    schema_object: str,
) -> None:
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / f'legacy-object-{revision}-{schema_object}.sqlite3'}",
        future=True,
    )
    migration = importlib.import_module(
        f"migrations.versions.{revision}_"
        + (
            "request_trace_fields"
            if revision == "0005"
            else "entity_projection_scope"
        )
    )
    with engine.begin() as connection:
        source_table = "events" if revision == "0005" else "entities"
        source_id = "event-one" if revision == "0005" else "entity-one"
        compat_table = (
            "_compat_0005_request_trace_fields"
            if revision == "0005"
            else "_compat_0006_entity_projection_scope"
        )
        connection.execute(
            sa.text(f"CREATE TABLE {source_table} (id VARCHAR(36) PRIMARY KEY)")
        )
        connection.execute(
            sa.text(f"INSERT INTO {source_table} VALUES (:source_id)"),
            {"source_id": source_id},
        )
        foreign_clause = (
            f"REFERENCES {source_table}(id)" if schema_object == "foreign" else ""
        )
        unique_clause = "UNIQUE" if schema_object == "unique" else ""
        check_clause = "CHECK (app_id <> '')" if schema_object == "check" else ""
        if revision == "0005":
            connection.execute(
                sa.text(
                    f"""
                    CREATE TABLE {compat_table} (
                        event_id VARCHAR(36) NOT NULL PRIMARY KEY {foreign_clause},
                        app_id VARCHAR(256) {unique_clause} {check_clause},
                        user_id VARCHAR(256), agent_id VARCHAR(256),
                        run_id VARCHAR(256), correlation_id VARCHAR(256),
                        latency_ms FLOAT, result_count BIGINT NOT NULL,
                        has_results INTEGER NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                sa.text(
                    f"""
                    INSERT INTO {compat_table}
                    VALUES ('event-one', 'app-a', NULL, NULL, NULL, NULL, NULL, 0, 0)
                    """
                )
            )
        else:
            connection.execute(
                sa.text(
                    f"""
                    CREATE TABLE {compat_table} (
                        entity_id VARCHAR(36) NOT NULL PRIMARY KEY {foreign_clause},
                        app_id VARCHAR(256) NOT NULL {unique_clause} {check_clause}
                    )
                    """
                )
            )
            connection.execute(
                sa.text(
                    f"INSERT INTO {compat_table} VALUES ('entity-one', 'app-a')"
                )
            )
        if schema_object == "index":
            connection.execute(
                sa.text(
                    f"CREATE INDEX ix_unexpected_compat_app ON {compat_table}(app_id)"
                )
            )
        monkeypatch.setattr(
            migration,
            "op",
            SimpleNamespace(get_bind=lambda: connection),
        )
        with pytest.raises(
            RuntimeError,
            match=rf"invalid {revision} compatibility snapshot structure",
        ):
            migration._validate_legacy_compatibility_snapshot()


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


def _prepare_compat_concurrency_db(tmp_path, revision: str, schedule: str):
    database_url = f"sqlite:///{tmp_path / f'{revision}-{schedule}.sqlite3'}"
    config = _alembic_config(database_url)
    command.upgrade(config, _compat_revision_name(revision))
    engine = sa.create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO projects (
                    id, name, default_app_id, mem0_base_url,
                    created_at, updated_at
                ) VALUES (
                    'compat-project', 'Compat Project', 'default-app',
                    'http://mem0:8000', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
    return config, engine


def _insert_compat_source_row(connection, revision: str, row_id: str) -> None:
    if revision == "0005":
        connection.execute(
            sa.text(
                """
                INSERT INTO events (
                    id, project_id, app_id, user_id, agent_id, run_id,
                    operation, status, request_json, response_json, error_json,
                    correlation_id, latency_ms, result_count, has_results,
                    created_at
                ) VALUES (
                    :row_id, 'compat-project', 'writer-app', 'writer-user',
                    'writer-agent', 'writer-run', 'memory.search', 'SUCCEEDED',
                    '{}', '{}', '{}', 'writer-correlation', 17.25, 9, 1,
                    CURRENT_TIMESTAMP
                )
                """
            ),
            {"row_id": row_id},
        )
        return
    connection.execute(
        sa.text(
            """
            INSERT INTO entities (
                id, project_id, app_id, entity_type, entity_id,
                display_name, metadata_json, memory_count,
                created_at, updated_at
            ) VALUES (
                :row_id, 'compat-project', 'writer-app', 'user', :row_id,
                'Writer Entity', '{}', 4, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        ),
        {"row_id": row_id},
    )


def _compat_source_values(connection, revision: str, row_id: str):
    if revision == "0005":
        return connection.execute(
            sa.text(
                """
                SELECT app_id, user_id, agent_id, run_id, correlation_id,
                       latency_ms, result_count, has_results
                FROM events WHERE id = :row_id
                """
            ),
            {"row_id": row_id},
        ).mappings().one_or_none()
    return connection.execute(
        sa.text(
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
            "app_id": "writer-app",
            "user_id": "writer-user",
            "agent_id": "writer-agent",
            "run_id": "writer-run",
            "correlation_id": "writer-correlation",
            "latency_ms": 17.25,
            "result_count": 9,
            "has_results": 1,
        }
    return {
        "app_id": "writer-app",
        "display_name": "Writer Entity",
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
                    sa.text(
                        "UPDATE alembic_version SET version_num = :revision"
                    ),
                    {"revision": target_revision},
                )
    finally:
        migration.op = original_op


@pytest.mark.parametrize("revision", ["0005", "0006"])
def test_compat_downgrade_writer_first_includes_committed_source_values(
    tmp_path,
    monkeypatch,
    revision: str,
) -> None:
    config, engine = _prepare_compat_concurrency_db(
        tmp_path,
        revision,
        "writer-first",
    )
    migration = _compat_migration(revision)
    snapshot_started = threading.Event()
    snapshot_finished = threading.Event()
    writer_inserted = threading.Event()
    release_writer = threading.Event()
    writer_committed = threading.Event()
    migration_errors: list[BaseException] = []
    writer_errors: list[BaseException] = []
    original_rebuild = migration._rebuild_compatibility_snapshot

    def observed_rebuild() -> None:
        snapshot_started.set()
        original_rebuild()
        snapshot_finished.set()

    monkeypatch.setattr(migration, "_rebuild_compatibility_snapshot", observed_rebuild)

    def writer() -> None:
        try:
            with engine.connect() as connection:
                transaction = connection.begin()
                _insert_compat_source_row(connection, revision, "writer-first-row")
                writer_inserted.set()
                assert release_writer.wait(5)
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
    writer_thread.start()
    assert writer_inserted.wait(3)
    migration_thread.start()
    assert snapshot_started.wait(3)
    snapshot_finished_before_commit = snapshot_finished.wait(0.35)
    release_writer.set()
    writer_thread.join(8)
    migration_thread.join(8)

    assert not writer_thread.is_alive()
    assert not migration_thread.is_alive()
    assert writer_errors == []
    assert migration_errors == []
    assert writer_committed.is_set()
    assert not snapshot_finished_before_commit, (
        f"{revision} snapshot completed before the earlier writer committed"
    )

    command.upgrade(config, _compat_revision_name(revision))
    with engine.connect() as connection:
        values = _compat_source_values(connection, revision, "writer-first-row")
    assert values == _expected_compat_source_values(revision)


@pytest.mark.parametrize("revision", ["0005", "0006"])
def test_compat_downgrade_migration_first_blocks_late_source_writer(
    tmp_path,
    monkeypatch,
    revision: str,
) -> None:
    config, engine = _prepare_compat_concurrency_db(
        tmp_path,
        revision,
        "migration-first",
    )
    migration = _compat_migration(revision)
    snapshot_ready = threading.Event()
    release_snapshot = threading.Event()
    writer_attempted = threading.Event()
    writer_committed = threading.Event()
    migration_errors: list[BaseException] = []
    writer_errors: list[BaseException] = []
    original_validate = migration._validate_compatibility_snapshot

    def pause_ready_snapshot(*, expected_source_rows=None, require_ready=True):
        result = original_validate(
            expected_source_rows=expected_source_rows,
            require_ready=require_ready,
        )
        if require_ready:
            snapshot_ready.set()
            assert release_snapshot.wait(5)
        return result

    monkeypatch.setattr(
        migration,
        "_validate_compatibility_snapshot",
        pause_ready_snapshot,
    )

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
                _insert_compat_source_row(connection, revision, "migration-first-row")
            writer_committed.set()
        except BaseException as exc:
            writer_errors.append(exc)

    migration_thread = threading.Thread(target=downgrade, daemon=True)
    writer_thread = threading.Thread(target=writer, daemon=True)
    migration_thread.start()
    assert snapshot_ready.wait(3)
    writer_thread.start()
    assert writer_attempted.wait(3)
    writer_committed_before_release = writer_committed.wait(0.35)
    release_snapshot.set()
    migration_thread.join(8)
    writer_thread.join(8)

    assert not migration_thread.is_alive()
    assert not writer_thread.is_alive()
    assert migration_errors == []

    command.upgrade(config, _compat_revision_name(revision))
    with engine.connect() as connection:
        values = _compat_source_values(connection, revision, "migration-first-row")

    if writer_committed_before_release:
        assert values == _expected_compat_source_values(revision), (
            f"{revision} late writer committed but its scoped values changed: {values}"
        )
    assert not writer_committed_before_release, (
        f"{revision} late writer committed after snapshot but before destructive DDL"
    )
    assert not writer_committed.is_set()
    assert writer_errors


@pytest.mark.parametrize(
    ("revision", "source_table"),
    [("0005", "events"), ("0006", "entities")],
)
def test_compat_snapshot_declares_source_writer_fence_before_snapshot_ddl(
    revision: str,
    source_table: str,
) -> None:
    migration = _compat_migration(revision)
    source = Path(migration.__file__).read_text()
    function_source = source.split(
        "def _rebuild_compatibility_snapshot() -> None:",
        1,
    )[1].split("\ndef ", 1)[0]

    assert f"LOCK TABLE {source_table} IN ACCESS EXCLUSIVE MODE" in function_source
    assert f"UPDATE {source_table}" in function_source
    assert function_source.index(f"LOCK TABLE {source_table}") < function_source.index(
        "CREATE TABLE"
    )
    assert function_source.index(f"UPDATE {source_table}") < function_source.index(
        "CREATE TABLE"
    )


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


def _prepare_mutation_downgrade_concurrency_db(tmp_path, name: str):
    database_url = f"sqlite:///{tmp_path / name}"
    config = _alembic_config(database_url)
    command.upgrade(config, "head")
    engine = sa.create_engine(
        database_url,
        future=True,
        connect_args={"timeout": 5},
    )
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
                    'concurrent-event', 'repo-a', 'app-a', 'memory.delete',
                    'FAILED', '{}', '{}', '{}', CURRENT_TIMESTAMP
                )
                """
            )
        )
    return engine


def _insert_unresolved_intent(connection) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO mutation_intents (
                id, project_id, app_id, event_id, operation, operation_key,
                status, payload_json, result_json, error_json, attempt_count,
                created_at, updated_at
            ) VALUES (
                'concurrent-intent', 'repo-a', 'app-a', 'concurrent-event',
                'memory.delete', 'concurrent-key', 'ACTIVE', '{}', '{}', '{}', 1,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        )
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO mutation_intent_targets (
                id, intent_id, memory_id, ordinal, status, error_json,
                created_at, updated_at
            ) VALUES (
                'concurrent-target', 'concurrent-intent', 'memory-one', 0,
                'PENDING', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        )
    )


def test_mutation_downgrade_waits_for_earlier_writer_then_refuses(
    tmp_path,
    monkeypatch,
) -> None:
    engine = _prepare_mutation_downgrade_concurrency_db(
        tmp_path,
        "writer-first-downgrade.sqlite3",
    )
    migration = importlib.import_module(
        "migrations.versions.0007_mutation_intents"
    )
    migration_op = _ThreadLocalMigrationOp()
    monkeypatch.setattr(migration, "op", migration_op)
    writer_inserted = threading.Event()
    release_writer = threading.Event()
    writer_committed = threading.Event()
    guard_counted = threading.Event()
    migration_errors: list[BaseException] = []
    writer_errors: list[BaseException] = []

    def writer() -> None:
        try:
            with engine.connect() as connection:
                transaction = connection.begin()
                _insert_unresolved_intent(connection)
                writer_inserted.set()
                assert release_writer.wait(5)
                transaction.commit()
                writer_committed.set()
        except BaseException as exc:
            writer_errors.append(exc)

    def downgrade() -> None:
        try:
            with engine.connect() as connection:
                with connection.begin():
                    migration_op.set_bind(
                        _GuardSignalingBind(
                            connection,
                            guard_counted=guard_counted,
                        )
                    )
                    migration.downgrade()
        except BaseException as exc:
            migration_errors.append(exc)

    writer_thread = threading.Thread(target=writer, daemon=True)
    migration_thread = threading.Thread(target=downgrade, daemon=True)
    writer_thread.start()
    assert writer_inserted.wait(3)
    migration_thread.start()
    guard_ran_before_writer_commit = guard_counted.wait(0.35)
    release_writer.set()
    writer_thread.join(8)
    migration_thread.join(8)

    assert not writer_thread.is_alive()
    assert not migration_thread.is_alive()
    assert writer_errors == []
    assert writer_committed.is_set()
    assert not guard_ran_before_writer_commit, (
        "downgrade counted before the earlier SQLite writer committed"
    )
    assert len(migration_errors) == 1
    assert isinstance(migration_errors[0], RuntimeError)
    assert "nonterminal mutation intents" in str(migration_errors[0])
    with engine.connect() as connection:
        assert {"mutation_intents", "mutation_intent_targets"}.issubset(
            sa.inspect(connection).get_table_names()
        )
        assert connection.scalar(
            sa.text("SELECT COUNT(*) FROM mutation_intents")
        ) == 1


def test_mutation_downgrade_lock_prevents_later_writer_from_being_dropped(
    tmp_path,
    monkeypatch,
) -> None:
    engine = _prepare_mutation_downgrade_concurrency_db(
        tmp_path,
        "migration-first-downgrade.sqlite3",
    )
    migration = importlib.import_module(
        "migrations.versions.0007_mutation_intents"
    )
    migration_op = _ThreadLocalMigrationOp()
    monkeypatch.setattr(migration, "op", migration_op)
    guard_counted = threading.Event()
    release_guard = threading.Event()
    writer_attempted = threading.Event()
    writer_committed = threading.Event()
    migration_errors: list[BaseException] = []
    writer_errors: list[BaseException] = []

    def downgrade() -> None:
        try:
            with engine.connect() as connection:
                with connection.begin():
                    migration_op.set_bind(
                        _GuardSignalingBind(
                            connection,
                            guard_counted=guard_counted,
                            release_guard=release_guard,
                        )
                    )
                    migration.downgrade()
        except BaseException as exc:
            migration_errors.append(exc)

    def writer() -> None:
        try:
            with engine.begin() as connection:
                writer_attempted.set()
                _insert_unresolved_intent(connection)
            writer_committed.set()
        except BaseException as exc:
            writer_errors.append(exc)

    migration_thread = threading.Thread(target=downgrade, daemon=True)
    writer_thread = threading.Thread(target=writer, daemon=True)
    migration_thread.start()
    assert guard_counted.wait(3)
    writer_thread.start()
    assert writer_attempted.wait(3)
    writer_committed_before_drop = writer_committed.wait(0.35)
    release_guard.set()
    migration_thread.join(8)
    writer_thread.join(8)

    assert not migration_thread.is_alive()
    assert not writer_thread.is_alive()
    assert migration_errors == []
    assert not writer_committed_before_drop, (
        "later SQLite writer committed between downgrade guard and DROP"
    )
    assert not writer_committed.is_set()
    assert writer_errors
    assert "mutation_intents" not in sa.inspect(engine).get_table_names()


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
