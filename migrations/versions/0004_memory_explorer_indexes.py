"""add memory explorer query indexes

Revision ID: 0004_memory_explorer_indexes
Revises: 0003_category_name_uniqueness
Create Date: 2026-07-13 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_memory_explorer_indexes"
down_revision: str | None = "0003_category_name_uniqueness"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MEMORY_EXPLORER_INDEXES = (
    (
        "ix_memories_index_project_active_created",
        ["project_id", "deleted_at", "created_at"],
    ),
    (
        "ix_memories_index_project_app_user",
        ["project_id", "app_id", "user_id"],
    ),
    (
        "ix_memories_index_project_app_agent",
        ["project_id", "app_id", "agent_id"],
    ),
    (
        "ix_memories_index_project_app_run",
        ["project_id", "app_id", "run_id"],
    ),
    (
        "ix_memories_index_project_category",
        ["project_id", "category"],
    ),
)


def upgrade() -> None:
    for name, columns in MEMORY_EXPLORER_INDEXES:
        op.create_index(name, "memories_index", columns, unique=False)


def downgrade() -> None:
    for name, _columns in reversed(MEMORY_EXPLORER_INDEXES):
        op.drop_index(name, table_name="memories_index")
