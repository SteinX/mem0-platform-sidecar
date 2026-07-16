"""add request trace query fields

Revision ID: 0005_request_trace_fields
Revises: 0004_memory_explorer_indexes
Create Date: 2026-07-13 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_request_trace_fields"
down_revision: str | None = "0004_memory_explorer_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REQUEST_TRACE_INDEXES = (
    ("ix_events_project_created", ["project_id", "created_at"]),
    (
        "ix_events_project_app_created",
        ["project_id", "app_id", "created_at"],
    ),
    (
        "ix_events_project_app_user_created",
        ["project_id", "app_id", "user_id", "created_at"],
    ),
    (
        "ix_events_project_app_agent_created",
        ["project_id", "app_id", "agent_id", "created_at"],
    ),
    (
        "ix_events_project_app_run_created",
        ["project_id", "app_id", "run_id", "created_at"],
    ),
    (
        "ix_events_project_operation_created",
        ["project_id", "operation", "created_at"],
    ),
    (
        "ix_events_project_status_created",
        ["project_id", "status", "created_at"],
    ),
    (
        "ix_events_project_has_results_created",
        ["project_id", "has_results", "created_at"],
    ),
)
COMPATIBILITY_TABLE = "_compat_0005_request_trace_fields"
COMPATIBILITY_COLUMNS = {
    "event_id",
    "app_id",
    "user_id",
    "agent_id",
    "run_id",
    "correlation_id",
    "latency_ms",
    "result_count",
    "has_results",
    "snapshot_kind",
    "snapshot_row_count",
}
LEGACY_COMPATIBILITY_COLUMNS = COMPATIBILITY_COLUMNS - {
    "snapshot_kind",
    "snapshot_row_count",
}
LEGACY_COMPATIBILITY_DESCRIPTORS = {
    "sqlite": (
        ("event_id", "VARCHAR", 36, False, None),
        ("app_id", "VARCHAR", 256, True, None),
        ("user_id", "VARCHAR", 256, True, None),
        ("agent_id", "VARCHAR", 256, True, None),
        ("run_id", "VARCHAR", 256, True, None),
        ("correlation_id", "VARCHAR", 256, True, None),
        ("latency_ms", "FLOAT", None, True, None),
        ("result_count", "BIGINT", None, False, None),
        ("has_results", "INTEGER", None, False, None),
    ),
    "postgresql": (
        ("event_id", "VARCHAR", 36, False, None),
        ("app_id", "VARCHAR", 256, True, None),
        ("user_id", "VARCHAR", 256, True, None),
        ("agent_id", "VARCHAR", 256, True, None),
        ("run_id", "VARCHAR", 256, True, None),
        ("correlation_id", "VARCHAR", 256, True, None),
        ("latency_ms", "DOUBLE_PRECISION", None, True, None),
        ("result_count", "BIGINT", None, False, None),
        ("has_results", "INTEGER", None, False, None),
    ),
}
SOURCE_COLUMNS = COMPATIBILITY_COLUMNS - {
    "event_id",
    "snapshot_kind",
    "snapshot_row_count",
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
        raise RuntimeError("invalid 0005 compatibility snapshot structure")
    counts = bind.execute(
        sa.text(
            f"""
            SELECT
                SUM(CASE WHEN snapshot_kind = 'DATA' THEN 1 ELSE 0 END)
                    AS data_rows,
                COUNT(DISTINCT CASE WHEN snapshot_kind = 'DATA' THEN event_id END)
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
        raise RuntimeError("invalid 0005 compatibility snapshot content")
    if expected_source_rows is not None and data_rows != expected_source_rows:
        raise RuntimeError("invalid 0005 compatibility snapshot row count")
    if require_ready and (
        ready_rows != 1 or counts["ready_row_count"] != data_rows
    ):
        raise RuntimeError("invalid 0005 compatibility snapshot READY marker")
    if not require_ready and ready_rows:
        raise RuntimeError("invalid 0005 compatibility snapshot staging state")
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
        == ["event_id"]
        and not _has_unexpected_legacy_indexes(bind, inspector)
        and not inspector.get_unique_constraints(COMPATIBILITY_TABLE)
        and not inspector.get_foreign_keys(COMPATIBILITY_TABLE)
        and not inspector.get_check_constraints(COMPATIBILITY_TABLE)
    )
    if not exact_structure:
        raise RuntimeError("invalid 0005 compatibility snapshot structure")
    counts = bind.execute(
        sa.text(
            f"""
            SELECT
                COUNT(*) AS snapshot_rows,
                COUNT(event_id) AS nonnull_ids,
                COUNT(DISTINCT event_id) AS distinct_ids
            FROM {COMPATIBILITY_TABLE}
            """
        )
    ).mappings().one()
    snapshot_rows = int(counts["snapshot_rows"] or 0)
    if (
        int(counts["nonnull_ids"] or 0) != snapshot_rows
        or int(counts["distinct_ids"] or 0) != snapshot_rows
    ):
        raise RuntimeError("invalid 0005 legacy compatibility snapshot content")
    source_rows = int(bind.scalar(sa.text("SELECT COUNT(*) FROM events")) or 0)
    if snapshot_rows == 0:
        if source_rows:
            raise RuntimeError(
                "ambiguous empty 0005 legacy compatibility snapshot"
            )
        return 0
    matched_rows = int(
        bind.scalar(
            sa.text(
                f"""
                SELECT COUNT(*)
                FROM {COMPATIBILITY_TABLE} AS compat
                JOIN events ON events.id = compat.event_id
                """
            )
        )
        or 0
    )
    if matched_rows != snapshot_rows:
        raise RuntimeError("invalid 0005 legacy compatibility snapshot content")
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
    raise RuntimeError("invalid 0005 compatibility snapshot structure")


def _rebuild_compatibility_snapshot() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("LOCK TABLE events IN ACCESS EXCLUSIVE MODE"))
    elif bind.dialect.name == "sqlite":
        op.execute(sa.text("UPDATE events SET id = id WHERE 0"))
    source_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("events")
    }
    if not SOURCE_COLUMNS.issubset(source_columns):
        raise RuntimeError(
            "cannot rebuild 0005 compatibility snapshot without source columns"
        )
    if _compatibility_table_exists():
        op.drop_table(COMPATIBILITY_TABLE)
    op.execute(
        sa.text(
            f"""
            CREATE TABLE {COMPATIBILITY_TABLE} AS
            SELECT
                id AS event_id,
                app_id,
                user_id,
                agent_id,
                run_id,
                correlation_id,
                latency_ms,
                result_count,
                has_results,
                CAST('DATA' AS VARCHAR(16)) AS snapshot_kind,
                CAST(NULL AS BIGINT) AS snapshot_row_count
            FROM events
            """
        )
    )
    source_rows = int(bind.scalar(sa.text("SELECT COUNT(*) FROM events")) or 0)
    data_rows = _validate_compatibility_snapshot(
        expected_source_rows=source_rows,
        require_ready=False,
    )
    op.execute(
        sa.text(
            f"""
            INSERT INTO {COMPATIBILITY_TABLE} (
                event_id, app_id, user_id, agent_id, run_id,
                correlation_id, latency_ms, result_count, has_results,
                snapshot_kind, snapshot_row_count
            ) VALUES (
                NULL, NULL, NULL, NULL, NULL,
                NULL, NULL, NULL, NULL,
                'READY', :row_count
            )
            """
        ).bindparams(row_count=data_rows)
    )
    _validate_compatibility_snapshot(expected_source_rows=source_rows)


def _restore_downgraded_trace_fields(snapshot_format: str | None) -> None:
    if snapshot_format is None:
        return
    data_predicate = (
        "AND compat.snapshot_kind = 'DATA'"
        if snapshot_format == "ready"
        else ""
    )
    assignments = ",\n".join(
        f"""{field_name} = (
                SELECT compat.{field_name}
                FROM {COMPATIBILITY_TABLE} AS compat
                WHERE compat.event_id = events.id
                  {data_predicate}
            )"""
        for field_name in (
            "app_id",
            "user_id",
            "agent_id",
            "run_id",
            "correlation_id",
            "latency_ms",
            "result_count",
            "has_results",
        )
    )
    op.execute(
        sa.text(
            f"""
            UPDATE events
            SET {assignments}
            WHERE EXISTS (
                SELECT 1
                FROM {COMPATIBILITY_TABLE} AS compat
                WHERE compat.event_id = events.id
                  {data_predicate}
            )
            """
        )
    )


def upgrade() -> None:
    snapshot_format = _compatibility_snapshot_format()
    op.add_column(
        "events",
        sa.Column("app_id", sa.String(length=256), nullable=True),
    )
    for field_name in ("user_id", "agent_id", "run_id"):
        op.add_column(
            "events",
            sa.Column(field_name, sa.String(length=256), nullable=True),
        )
    op.add_column(
        "events",
        sa.Column("correlation_id", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "events",
        sa.Column("latency_ms", sa.Float(), nullable=True),
    )
    op.add_column(
        "events",
        sa.Column(
            "result_count",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "events",
        sa.Column(
            "has_results",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    _restore_downgraded_trace_fields(snapshot_format)
    for name, columns in REQUEST_TRACE_INDEXES:
        op.create_index(name, "events", columns, unique=False)
    if _compatibility_table_exists():
        op.drop_table(COMPATIBILITY_TABLE)


def downgrade() -> None:
    if _compatibility_table_exists() is not None:
        _rebuild_compatibility_snapshot()
    for name, _columns in reversed(REQUEST_TRACE_INDEXES):
        op.drop_index(name, table_name="events")
    op.drop_column("events", "has_results")
    op.drop_column("events", "result_count")
    op.drop_column("events", "latency_ms")
    op.drop_column("events", "correlation_id")
    op.drop_column("events", "run_id")
    op.drop_column("events", "agent_id")
    op.drop_column("events", "user_id")
    op.drop_column("events", "app_id")
