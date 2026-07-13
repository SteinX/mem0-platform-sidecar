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
    "ix_events_project_operation_created",
    "ix_events_project_status_created",
    "ix_events_project_has_results_created",
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
    assert {"correlation_id", "latency_ms", "result_count", "has_results"} <= set(
        columns
    )
    assert columns["result_count"]["nullable"] is False
    assert columns["has_results"]["nullable"] is False
    assert str(columns["result_count"]["default"]).strip("'()") == "0"
    assert str(columns["has_results"]["default"]).strip("'()") == "0"
    assert REQUEST_TRACE_INDEXES <= {
        index["name"] for index in inspector.get_indexes("events")
    }

    with engine.connect() as connection:
        legacy = connection.execute(
            sa.text(
                """
                SELECT correlation_id, latency_ms, result_count, has_results
                FROM events
                WHERE id = 'event-legacy'
                """
            )
        ).mappings().one()
    assert legacy == {
        "correlation_id": None,
        "latency_ms": None,
        "result_count": 0,
        "has_results": 0,
    }

    command.downgrade(config, "0004_memory_explorer_indexes")

    downgraded = sa.inspect(engine)
    assert {"correlation_id", "latency_ms", "result_count", "has_results"}.isdisjoint(
        column["name"] for column in downgraded.get_columns("events")
    )
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
    assert isinstance(result_count.type, sa.BigInteger)
    assert result_count.type.compile(dialect=postgresql.dialect()) == "BIGINT"
