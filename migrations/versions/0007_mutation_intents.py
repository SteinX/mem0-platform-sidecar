"""add durable mutation intents

Revision ID: 0007_mutation_intents
Revises: 0006_entity_projection_scope
Create Date: 2026-07-15 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_mutation_intents"
down_revision: str | None = "0006_entity_projection_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
TERMINAL_INTENT_STATUSES = ("COMPLETED", "FAILED", "PARTIAL")


def upgrade() -> None:
    op.create_table(
        "mutation_intents",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=128),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column("app_id", sa.String(length=256), nullable=False),
        sa.Column(
            "event_id",
            sa.String(length=36),
            sa.ForeignKey("events.id"),
            nullable=False,
        ),
        sa.Column("operation", sa.String(length=128), nullable=False),
        sa.Column("operation_key", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="ACTIVE",
        ),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("result_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("error_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "project_id",
            "app_id",
            "operation",
            "operation_key",
            name="uq_mutation_intents_scope_operation_key",
        ),
    )
    op.create_index(
        "ix_mutation_intents_scope_status_created",
        "mutation_intents",
        ["project_id", "app_id", "status", "created_at"],
        unique=False,
    )
    op.create_table(
        "mutation_intent_targets",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "intent_id",
            sa.String(length=36),
            sa.ForeignKey("mutation_intents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("memory_id", sa.String(length=256), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("error_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "intent_id",
            "memory_id",
            name="uq_mutation_intent_targets_intent_memory",
        ),
    )
    op.create_index(
        "ix_mutation_intent_targets_intent_status_ordinal",
        "mutation_intent_targets",
        ["intent_id", "status", "ordinal"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        bind.execute(
            sa.text(
                """
                UPDATE mutation_intents
                SET updated_at = updated_at
                WHERE 0 = 1
                """
            )
        )
    elif bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                """
                LOCK TABLE mutation_intents, mutation_intent_targets
                IN ACCESS EXCLUSIVE MODE
                """
            )
        )
    unresolved_count = int(
        bind.scalar(
            sa.text(
                """
                SELECT COUNT(*)
                FROM mutation_intents
                WHERE status NOT IN ('COMPLETED', 'FAILED', 'PARTIAL')
                """
            )
        )
        or 0
    )
    if unresolved_count:
        raise RuntimeError(
            "cannot downgrade 0007 while nonterminal mutation intents remain; "
            "recover or explicitly terminalize them first"
        )
    op.drop_index(
        "ix_mutation_intent_targets_intent_status_ordinal",
        table_name="mutation_intent_targets",
    )
    op.drop_table("mutation_intent_targets")
    op.drop_index(
        "ix_mutation_intents_scope_status_created",
        table_name="mutation_intents",
    )
    op.drop_table("mutation_intents")
