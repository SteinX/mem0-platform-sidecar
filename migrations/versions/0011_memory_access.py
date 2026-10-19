from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_memory_access"
down_revision: str | None = "0010_add_recovery_scan_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("memories_index") as batch:
        batch.add_column(
            sa.Column("access_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("last_accessed_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    with op.batch_alter_table("memories_index") as batch:
        batch.drop_column("last_accessed_at")
        batch.drop_column("access_count")
