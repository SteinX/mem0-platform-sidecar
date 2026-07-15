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


def _compatibility_table_exists() -> bool | None:
    get_bind = getattr(op, "get_bind", None)
    return sa.inspect(get_bind()).has_table(COMPATIBILITY_TABLE) if get_bind else None


def upgrade() -> None:
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
    if _compatibility_table_exists():
        op.execute(
            sa.text(
                f"""
                UPDATE entities
                SET app_id = (
                    SELECT compat.app_id
                    FROM {COMPATIBILITY_TABLE} AS compat
                    WHERE compat.entity_id = entities.id
                )
                WHERE EXISTS (
                    SELECT 1
                    FROM {COMPATIBILITY_TABLE} AS compat
                    WHERE compat.entity_id = entities.id
                )
                """
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
    if _compatibility_table_exists() is False:
        op.create_table(
            COMPATIBILITY_TABLE,
            sa.Column("entity_id", sa.String(length=36), primary_key=True),
            sa.Column("app_id", sa.String(length=256), nullable=False),
        )
        op.execute(
            sa.text(
                f"""
                INSERT INTO {COMPATIBILITY_TABLE} (entity_id, app_id)
                SELECT id, app_id FROM entities
                """
            )
        )
    for name, _columns in reversed(ENTITY_PROJECTION_INDEXES):
        op.drop_index(name, table_name="entities")
    with op.batch_alter_table("entities") as batch_op:
        batch_op.drop_constraint(
            "uq_entities_project_app_type_id",
            type_="unique",
        )
        batch_op.drop_column("app_id")
