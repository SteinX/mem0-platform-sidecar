"""add mutation recovery scan index

Revision ID: 0010_add_recovery_scan_index
Revises: 0009_request_channel_attribution
Create Date: 2026-09-22 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_add_recovery_scan_index"
down_revision: str | None = "0009_request_channel_attribution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ix_mutation_intents_add_recovery_scan"
INDEX_COLUMNS = (
    "operation",
    "status",
    "updated_at",
    "project_id",
    "app_id",
    "lease_expires_at",
)


def upgrade() -> None:
    op.create_index(INDEX_NAME, "mutation_intents", INDEX_COLUMNS, unique=False)


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="mutation_intents")
