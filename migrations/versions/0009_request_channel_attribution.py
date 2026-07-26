"""add request channel attribution

Revision ID: 0009_request_channel_attribution
Revises: 0008_memory_consolidation
Create Date: 2026-07-26 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_request_channel_attribution"
down_revision: str | None = "0008_memory_consolidation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ix_events_project_app_channel_created"


def upgrade() -> None:
    with op.batch_alter_table("events") as batch_op:
        batch_op.add_column(sa.Column("request_transport", sa.String(length=16)))
        batch_op.add_column(sa.Column("credential_kind", sa.String(length=32)))
        batch_op.add_column(sa.Column("credential_id", sa.String(length=36)))
        batch_op.add_column(sa.Column("credential_label", sa.String(length=255)))
        batch_op.add_column(sa.Column("credential_prefix", sa.String(length=12)))
        batch_op.create_index(
            INDEX_NAME,
            [
                "project_id",
                "app_id",
                "request_transport",
                "credential_kind",
                "credential_id",
                "created_at",
            ],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("events") as batch_op:
        batch_op.drop_index(INDEX_NAME)
        batch_op.drop_column("credential_prefix")
        batch_op.drop_column("credential_label")
        batch_op.drop_column("credential_id")
        batch_op.drop_column("credential_kind")
        batch_op.drop_column("request_transport")
