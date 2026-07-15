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
SOURCE_COLUMNS = COMPATIBILITY_COLUMNS - {
    "event_id",
    "snapshot_kind",
    "snapshot_row_count",
}


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
    if not COMPATIBILITY_COLUMNS.issubset(columns):
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


def _rebuild_compatibility_snapshot() -> None:
    bind = op.get_bind()
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


def _restore_downgraded_trace_fields() -> None:
    if _compatibility_table_exists() is not True:
        return
    assignments = ",\n".join(
        f"""{field_name} = (
                SELECT compat.{field_name}
                FROM {COMPATIBILITY_TABLE} AS compat
                WHERE compat.event_id = events.id
                  AND compat.snapshot_kind = 'DATA'
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
                  AND compat.snapshot_kind = 'DATA'
            )
            """
        )
    )


def upgrade() -> None:
    if _compatibility_table_exists() is True:
        _validate_compatibility_snapshot()
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
    _restore_downgraded_trace_fields()
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
