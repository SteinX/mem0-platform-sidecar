"""scope entity projections by project and app

Revision ID: 0006_entity_projection_scope
Revises: 0005_request_trace_fields
Create Date: 2026-07-13 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_entity_projection_scope"
down_revision: str | None = "0005_request_trace_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENTITY_PROJECTION_INDEXES = (
    (
        "ix_entities_project_app_type_updated",
        ["project_id", "app_id", "entity_type", "updated_at"],
    ),
    (
        "ix_entities_project_app_last_seen",
        ["project_id", "app_id", "last_seen_at"],
    ),
)
COMPATIBILITY_TABLE = "_compat_0006_entity_projection_scope"
COMPATIBILITY_COLUMNS = {
    "entity_id",
    "app_id",
    "snapshot_kind",
    "snapshot_row_count",
}
LEGACY_COMPATIBILITY_COLUMNS = {"entity_id", "app_id"}
LEGACY_COMPATIBILITY_DESCRIPTORS = {
    "sqlite": (
        ("entity_id", "VARCHAR", 36, False, None),
        ("app_id", "VARCHAR", 256, False, None),
    ),
    "postgresql": (
        ("entity_id", "VARCHAR", 36, False, None),
        ("app_id", "VARCHAR", 256, False, None),
    ),
}


def _has_unexpected_legacy_indexes(
    bind: sa.engine.Connection,
    inspector: sa.Inspector,
) -> bool:
    if bind.dialect.name == "sqlite":
        index_rows = bind.execute(
            sa.text(f"PRAGMA index_list('{COMPATIBILITY_TABLE}')")
        ).mappings()
        return any(index["origin"] != "pk" for index in index_rows)
    return bool(inspector.get_indexes(COMPATIBILITY_TABLE))


def _compatibility_table_exists() -> bool | None:
    get_bind = getattr(op, "get_bind", None)
    return sa.inspect(get_bind()).has_table(COMPATIBILITY_TABLE) if get_bind else None


def _validate_compatibility_snapshot(
    *,
    expected_source_rows: int | None = None,
    require_ready: bool = True,
) -> int:
    bind = op.get_bind()
    columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns(COMPATIBILITY_TABLE)
    }
    if columns != COMPATIBILITY_COLUMNS:
        raise RuntimeError("invalid 0006 compatibility snapshot structure")
    counts = bind.execute(
        sa.text(
            f"""
            SELECT
                SUM(CASE WHEN snapshot_kind = 'DATA' THEN 1 ELSE 0 END)
                    AS data_rows,
                COUNT(DISTINCT CASE WHEN snapshot_kind = 'DATA' THEN entity_id END)
                    AS distinct_data_ids,
                SUM(CASE WHEN snapshot_kind = 'READY' THEN 1 ELSE 0 END)
                    AS ready_rows,
                SUM(CASE WHEN snapshot_kind NOT IN ('DATA', 'READY') THEN 1 ELSE 0 END)
                    AS invalid_rows,
                MAX(CASE WHEN snapshot_kind = 'READY' THEN snapshot_row_count END)
                    AS ready_row_count
            FROM {COMPATIBILITY_TABLE}
            """
        )
    ).mappings().one()
    data_rows = int(counts["data_rows"] or 0)
    distinct_data_ids = int(counts["distinct_data_ids"] or 0)
    ready_rows = int(counts["ready_rows"] or 0)
    invalid_rows = int(counts["invalid_rows"] or 0)
    if invalid_rows or distinct_data_ids != data_rows:
        raise RuntimeError("invalid 0006 compatibility snapshot content")
    if expected_source_rows is not None and data_rows != expected_source_rows:
        raise RuntimeError("invalid 0006 compatibility snapshot row count")
    if require_ready and (
        ready_rows != 1 or counts["ready_row_count"] != data_rows
    ):
        raise RuntimeError("invalid 0006 compatibility snapshot READY marker")
    if not require_ready and ready_rows:
        raise RuntimeError("invalid 0006 compatibility snapshot staging state")
    return data_rows


def _validate_legacy_compatibility_snapshot() -> int:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    column_rows = inspector.get_columns(COMPATIBILITY_TABLE)
    expected_descriptor = LEGACY_COMPATIBILITY_DESCRIPTORS.get(bind.dialect.name)
    actual_descriptor = tuple(
        (
            column["name"],
            type(column["type"]).__name__.upper(),
            getattr(column["type"], "length", None),
            column["nullable"],
            column.get("default"),
        )
        for column in column_rows
    )
    exact_structure = (
        expected_descriptor is not None
        and actual_descriptor == expected_descriptor
        and inspector.get_pk_constraint(COMPATIBILITY_TABLE).get(
            "constrained_columns"
        )
        == ["entity_id"]
        and not _has_unexpected_legacy_indexes(bind, inspector)
        and not inspector.get_unique_constraints(COMPATIBILITY_TABLE)
        and not inspector.get_foreign_keys(COMPATIBILITY_TABLE)
        and not inspector.get_check_constraints(COMPATIBILITY_TABLE)
    )
    if not exact_structure:
        raise RuntimeError("invalid 0006 compatibility snapshot structure")
    counts = bind.execute(
        sa.text(
            f"""
            SELECT
                COUNT(*) AS snapshot_rows,
                COUNT(entity_id) AS nonnull_ids,
                COUNT(DISTINCT entity_id) AS distinct_ids,
                COUNT(app_id) AS nonnull_app_ids
            FROM {COMPATIBILITY_TABLE}
            """
        )
    ).mappings().one()
    snapshot_rows = int(counts["snapshot_rows"] or 0)
    if (
        int(counts["nonnull_ids"] or 0) != snapshot_rows
        or int(counts["distinct_ids"] or 0) != snapshot_rows
        or int(counts["nonnull_app_ids"] or 0) != snapshot_rows
    ):
        raise RuntimeError("invalid 0006 legacy compatibility snapshot content")
    source_rows = int(bind.scalar(sa.text("SELECT COUNT(*) FROM entities")) or 0)
    if snapshot_rows == 0:
        if source_rows:
            raise RuntimeError(
                "ambiguous empty 0006 legacy compatibility snapshot"
            )
        return 0
    matched_rows = int(
        bind.scalar(
            sa.text(
                f"""
                SELECT COUNT(*)
                FROM {COMPATIBILITY_TABLE} AS compat
                JOIN entities ON entities.id = compat.entity_id
                """
            )
        )
        or 0
    )
    if matched_rows != snapshot_rows:
        raise RuntimeError("invalid 0006 legacy compatibility snapshot content")
    return snapshot_rows


def _compatibility_snapshot_format() -> str | None:
    if _compatibility_table_exists() is not True:
        return None
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(COMPATIBILITY_TABLE)
    }
    if columns == COMPATIBILITY_COLUMNS:
        _validate_compatibility_snapshot()
        return "ready"
    if columns == LEGACY_COMPATIBILITY_COLUMNS:
        _validate_legacy_compatibility_snapshot()
        return "legacy"
    raise RuntimeError("invalid 0006 compatibility snapshot structure")


def _rebuild_compatibility_snapshot() -> None:
    bind = op.get_bind()
    source_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("entities")
    }
    if "app_id" not in source_columns:
        raise RuntimeError(
            "cannot rebuild 0006 compatibility snapshot without source columns"
        )
    if _compatibility_table_exists():
        op.drop_table(COMPATIBILITY_TABLE)
    op.execute(
        sa.text(
            f"""
            CREATE TABLE {COMPATIBILITY_TABLE} AS
            SELECT
                id AS entity_id,
                app_id,
                CAST('DATA' AS VARCHAR(16)) AS snapshot_kind,
                CAST(NULL AS BIGINT) AS snapshot_row_count
            FROM entities
            """
        )
    )
    source_rows = int(bind.scalar(sa.text("SELECT COUNT(*) FROM entities")) or 0)
    data_rows = _validate_compatibility_snapshot(
        expected_source_rows=source_rows,
        require_ready=False,
    )
    op.execute(
        sa.text(
            f"""
            INSERT INTO {COMPATIBILITY_TABLE} (
                entity_id, app_id, snapshot_kind, snapshot_row_count
            ) VALUES (NULL, NULL, 'READY', :row_count)
            """
        ).bindparams(row_count=data_rows)
    )
    _validate_compatibility_snapshot(expected_source_rows=source_rows)


def upgrade() -> None:
    snapshot_format = _compatibility_snapshot_format()
    op.add_column(
        "entities",
        sa.Column("app_id", sa.String(length=256), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE entities
            SET app_id = COALESCE(
                (
                    SELECT projects.default_app_id
                    FROM projects
                    WHERE projects.id = entities.project_id
                ),
                entities.project_id
            )
            """
        )
    )
    if snapshot_format is not None:
        data_predicate = (
            "AND compat.snapshot_kind = 'DATA'"
            if snapshot_format == "ready"
            else ""
        )
        op.execute(
            sa.text(
                f"""
                UPDATE entities
                SET app_id = (
                    SELECT compat.app_id
                    FROM {COMPATIBILITY_TABLE} AS compat
                WHERE compat.entity_id = entities.id
                  {data_predicate}
                )
                WHERE EXISTS (
                    SELECT 1
                    FROM {COMPATIBILITY_TABLE} AS compat
                    WHERE compat.entity_id = entities.id
                      {data_predicate}
                )
                """.format(data_predicate=data_predicate)
            )
        )
    op.execute(
        sa.text(
            """
            DELETE FROM entities
            WHERE id IN (
                SELECT id
                FROM (
                    SELECT
                        id,
                        ROW_NUMBER() OVER (
                            PARTITION BY
                                project_id,
                                app_id,
                                entity_type,
                                entity_id
                            ORDER BY updated_at DESC, created_at DESC, id DESC
                        ) AS duplicate_rank
                    FROM entities
                ) AS ranked_entities
                WHERE duplicate_rank > 1
            )
            """
        )
    )
    with op.batch_alter_table("entities") as batch_op:
        batch_op.alter_column(
            "app_id",
            existing_type=sa.String(length=256),
            nullable=False,
        )
        batch_op.create_unique_constraint(
            "uq_entities_project_app_type_id",
            ["project_id", "app_id", "entity_type", "entity_id"],
        )
    for name, columns in ENTITY_PROJECTION_INDEXES:
        op.create_index(name, "entities", columns, unique=False)
    if _compatibility_table_exists():
        op.drop_table(COMPATIBILITY_TABLE)


def downgrade() -> None:
    if _compatibility_table_exists() is not None:
        _rebuild_compatibility_snapshot()
    for name, _columns in reversed(ENTITY_PROJECTION_INDEXES):
        op.drop_index(name, table_name="entities")
    with op.batch_alter_table("entities") as batch_op:
        batch_op.drop_constraint(
            "uq_entities_project_app_type_id",
            type_="unique",
        )
        batch_op.drop_column("app_id")
