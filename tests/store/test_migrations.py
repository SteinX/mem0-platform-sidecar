import importlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
