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


def upgrade() -> None:
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
    for name, columns in REQUEST_TRACE_INDEXES:
        op.create_index(name, "events", columns, unique=False)


def downgrade() -> None:
    for name, _columns in reversed(REQUEST_TRACE_INDEXES):
        op.drop_index(name, table_name="events")
    op.drop_column("events", "has_results")
    op.drop_column("events", "result_count")
    op.drop_column("events", "latency_ms")
    op.drop_column("events", "correlation_id")
