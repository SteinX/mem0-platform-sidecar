"""enforce category name uniqueness per project

Revision ID: 0003_category_name_uniqueness
Revises: 0002_export_jobs
Create Date: 2026-07-12 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_category_name_uniqueness"
down_revision: str | None = "0002_export_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATEGORY_NAME_UNIQUE_CONSTRAINT = "uq_categories_project_id_name"


def upgrade() -> None:
    duplicate = op.get_bind().execute(
        sa.text(
            """
            SELECT project_id, name
            FROM categories
            GROUP BY project_id, name
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "Cannot enforce category name uniqueness because duplicate category "
            "names per project already exist"
        )

    with op.batch_alter_table("categories") as batch_op:
        batch_op.create_unique_constraint(
            CATEGORY_NAME_UNIQUE_CONSTRAINT,
            ["project_id", "name"],
        )


def downgrade() -> None:
    with op.batch_alter_table("categories") as batch_op:
        batch_op.drop_constraint(
            CATEGORY_NAME_UNIQUE_CONSTRAINT,
            type_="unique",
        )
