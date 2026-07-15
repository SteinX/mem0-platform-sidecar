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


def _compatibility_table_exists() -> bool | None:
    get_bind = getattr(op, "get_bind", None)
    return sa.inspect(get_bind()).has_table(COMPATIBILITY_TABLE) if get_bind else None


def _restore_downgraded_trace_fields() -> None:
    if _compatibility_table_exists() is not True:
        return
    assignments = ",\n".join(
        f"""{field_name} = (
                SELECT compat.{field_name}
                FROM {COMPATIBILITY_TABLE} AS compat
                WHERE compat.event_id = events.id
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
            )
            """
        )
    )


def upgrade() -> None:
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
    if _compatibility_table_exists() is False:
        op.create_table(
            COMPATIBILITY_TABLE,
            sa.Column("event_id", sa.String(length=36), primary_key=True),
            sa.Column("app_id", sa.String(length=256), nullable=True),
            sa.Column("user_id", sa.String(length=256), nullable=True),
            sa.Column("agent_id", sa.String(length=256), nullable=True),
            sa.Column("run_id", sa.String(length=256), nullable=True),
            sa.Column("correlation_id", sa.String(length=256), nullable=True),
            sa.Column("latency_ms", sa.Float(), nullable=True),
            sa.Column("result_count", sa.BigInteger(), nullable=False),
            sa.Column("has_results", sa.Integer(), nullable=False),
        )
        op.execute(
            sa.text(
                f"""
                INSERT INTO {COMPATIBILITY_TABLE} (
                    event_id, app_id, user_id, agent_id, run_id,
                    correlation_id, latency_ms, result_count, has_results
                )
                SELECT
                    id, app_id, user_id, agent_id, run_id,
                    correlation_id, latency_ms, result_count, has_results
                FROM events
                """
            )
        )
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
