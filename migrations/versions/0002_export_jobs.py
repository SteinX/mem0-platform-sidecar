"""add export jobs

Revision ID: 0002_export_jobs
Revises: 0001_control_plane_core
Create Date: 2026-07-05 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_export_jobs"
down_revision: str | None = "0001_control_plane_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


export_status = sa.Enum(
    "PENDING",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    name="exportstatus",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        export_status.create(bind, checkfirst=True)

    op.create_table(
        "export_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("status", export_status, nullable=False),
        sa.Column("format", sa.String(length=32), nullable=False),
        sa.Column("filters_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("result_ref", sa.String(length=1024), nullable=True),
        sa.Column("error_json", sa.Text(), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("exported_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_table("export_jobs")
    if bind.dialect.name != "sqlite":
        export_status.drop(bind, checkfirst=True)
