"""add server-side memory consolidation control plane

Revision ID: 0008_memory_consolidation
Revises: 0007_mutation_intents
Create Date: 2026-07-23 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_memory_consolidation"
down_revision: str | None = "0007_mutation_intents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(
            sa.Column("result_json", sa.Text(), nullable=False, server_default="{}")
        )
        batch_op.add_column(sa.Column("dedupe_key", sa.String(length=256)))
        batch_op.add_column(
            sa.Column("lease_expires_at", sa.DateTime(timezone=True))
        )
        batch_op.create_unique_constraint(
            "uq_jobs_project_type_dedupe",
            ["project_id", "job_type", "dedupe_key"],
        )

    with op.batch_alter_table("memories_index") as batch_op:
        batch_op.add_column(sa.Column("content_hash", sa.String(length=64)))
        batch_op.add_column(sa.Column("content_length", sa.Integer()))
        batch_op.add_column(sa.Column("normalized_type", sa.String(length=128)))
        batch_op.add_column(sa.Column("source", sa.String(length=128)))
        batch_op.add_column(
            sa.Column(
                "pinned",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(sa.Column("expires_at", sa.DateTime(timezone=True)))
        batch_op.add_column(
            sa.Column("last_observed_at", sa.DateTime(timezone=True))
        )
        batch_op.add_column(
            sa.Column(
                "consolidation_state",
                sa.String(length=32),
                nullable=False,
                server_default="ACTIVE",
            )
        )
        batch_op.add_column(
            sa.Column("shadowed_by_proposal_id", sa.String(length=36))
        )
        batch_op.create_index(
            "ix_memories_index_consolidation_exact",
            ["project_id", "app_id", "normalized_type", "content_hash"],
            unique=False,
        )
        batch_op.create_index(
            "ix_memories_index_consolidation_dirty",
            [
                "project_id",
                "app_id",
                "consolidation_state",
                "last_observed_at",
            ],
            unique=False,
        )

    op.create_table(
        "consolidation_policies",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=128),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column("app_id", sa.String(length=256), nullable=False),
        sa.Column("enabled", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "mode", sa.String(length=32), nullable=False, server_default="OBSERVE"
        ),
        sa.Column("policy_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "project_id", "app_id", name="uq_consolidation_policy_scope"
        ),
    )

    op.create_table(
        "consolidation_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=128),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column("app_id", sa.String(length=256), nullable=False),
        sa.Column(
            "policy_id",
            sa.String(length=36),
            sa.ForeignKey("consolidation_policies.id"),
        ),
        sa.Column(
            "policy_version", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="PENDING"
        ),
        sa.Column("scan_cutoff", sa.DateTime(timezone=True)),
        sa.Column("counts_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("error_code", sa.String(length=64)),
        sa.Column("error_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_consolidation_runs_scope_status_created",
        "consolidation_runs",
        ["project_id", "app_id", "status", "created_at"],
        unique=False,
    )

    op.create_table(
        "consolidation_proposals",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("consolidation_runs.id"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.String(length=128),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column("app_id", sa.String(length=256), nullable=False),
        sa.Column("proposal_key", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="PENDING"
        ),
        sa.Column("source_ids_json", sa.Text(), nullable=False),
        sa.Column("canonical_memory_id", sa.String(length=256)),
        sa.Column("score", sa.Float()),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "expected_hashes_json", sa.Text(), nullable=False, server_default="{}"
        ),
        sa.Column(
            "export_job_id",
            sa.String(length=36),
            sa.ForeignKey("export_jobs.id"),
        ),
        sa.Column("not_before", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "run_id",
            "proposal_key",
            name="uq_consolidation_proposals_run_key",
        ),
    )
    op.create_index(
        "ix_consolidation_proposals_scope_status",
        "consolidation_proposals",
        ["project_id", "app_id", "status", "created_at"],
        unique=False,
    )

    op.create_table(
        "consolidation_lineage",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=128),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column("app_id", sa.String(length=256), nullable=False),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("consolidation_runs.id"),
            nullable=False,
        ),
        sa.Column(
            "proposal_id",
            sa.String(length=36),
            sa.ForeignKey("consolidation_proposals.id"),
            nullable=False,
        ),
        sa.Column("source_memory_id", sa.String(length=256), nullable=False),
        sa.Column("canonical_memory_id", sa.String(length=256)),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("source_content_hash", sa.String(length=64)),
        sa.Column(
            "export_job_id",
            sa.String(length=36),
            sa.ForeignKey("export_jobs.id"),
        ),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source_memory_id",
            "proposal_id",
            name="uq_consolidation_lineage_source_proposal",
        ),
    )
    op.create_index(
        "ix_consolidation_lineage_scope_applied",
        "consolidation_lineage",
        ["project_id", "app_id", "applied_at"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        bind.execute(
            sa.text(
                """
                UPDATE consolidation_runs
                SET updated_at = updated_at
                WHERE 0 = 1
                """
            )
        )
    elif bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                """
                LOCK TABLE consolidation_runs, consolidation_proposals,
                    consolidation_lineage, consolidation_policies
                IN ACCESS EXCLUSIVE MODE
                """
            )
        )

    nonterminal_count = int(
        bind.scalar(
            sa.text(
                """
                SELECT
                    (SELECT COUNT(*) FROM consolidation_runs
                     WHERE status IN ('PENDING', 'RUNNING'))
                  + (SELECT COUNT(*) FROM consolidation_proposals
                     WHERE status IN ('APPROVED', 'SHADOWED'))
                """
            )
        )
        or 0
    )
    if nonterminal_count:
        raise RuntimeError(
            "cannot downgrade 0008 while nonterminal consolidation work remains; "
            "complete, reject, or cancel it first"
        )

    op.drop_index(
        "ix_consolidation_lineage_scope_applied",
        table_name="consolidation_lineage",
    )
    op.drop_table("consolidation_lineage")
    op.drop_index(
        "ix_consolidation_proposals_scope_status",
        table_name="consolidation_proposals",
    )
    op.drop_table("consolidation_proposals")
    op.drop_index(
        "ix_consolidation_runs_scope_status_created",
        table_name="consolidation_runs",
    )
    op.drop_table("consolidation_runs")
    op.drop_table("consolidation_policies")

    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_constraint(
            "uq_jobs_project_type_dedupe", type_="unique"
        )
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("dedupe_key")
        batch_op.drop_column("result_json")

    with op.batch_alter_table("memories_index") as batch_op:
        batch_op.drop_index("ix_memories_index_consolidation_dirty")
        batch_op.drop_index("ix_memories_index_consolidation_exact")
        batch_op.drop_column("shadowed_by_proposal_id")
        batch_op.drop_column("consolidation_state")
        batch_op.drop_column("last_observed_at")
        batch_op.drop_column("expires_at")
        batch_op.drop_column("pinned")
        batch_op.drop_column("source")
        batch_op.drop_column("normalized_type")
        batch_op.drop_column("content_length")
        batch_op.drop_column("content_hash")
