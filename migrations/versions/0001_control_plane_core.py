import sqlalchemy as sa
from alembic import op

revision = "0001_control_plane_core"
down_revision = None
branch_labels = None
depends_on = None

event_status = sa.Enum(
    "PENDING",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    name="eventstatus",
)
job_status = sa.Enum(
    "PENDING",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    name="jobstatus",
)


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("default_user_id", sa.String(length=256)),
        sa.Column("default_app_id", sa.String(length=256)),
        sa.Column("default_agent_id", sa.String(length=256)),
        sa.Column("mem0_base_url", sa.String(length=1024), nullable=False),
        sa.Column("mem0_api_key_ref", sa.String(length=256)),
        sa.Column("settings_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("key_hash", sa.String(length=128), nullable=False, unique=True),
        sa.Column("prefix", sa.String(length=32), nullable=False),
        sa.Column(
            "project_id",
            sa.String(length=128),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("scopes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "categories",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=128),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("schema_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "strategy",
            sa.String(length=64),
            nullable=False,
            server_default="metadata",
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "memories_index",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=128),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column("mem0_memory_id", sa.String(length=256), nullable=False),
        sa.Column("user_id", sa.String(length=256)),
        sa.Column("agent_id", sa.String(length=256)),
        sa.Column("app_id", sa.String(length=256)),
        sa.Column("run_id", sa.String(length=256)),
        sa.Column("category", sa.String(length=128)),
        sa.Column("entity_refs_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column(
            "metadata_projection_json",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "project_id",
            "mem0_memory_id",
            name="uq_memories_index_project_mem0_memory_id",
        ),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=128),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column("operation", sa.String(length=128), nullable=False),
        sa.Column("status", event_status, nullable=False),
        sa.Column("subject_type", sa.String(length=128)),
        sa.Column("subject_id", sa.String(length=256)),
        sa.Column("request_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("response_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("error_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "entities",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=128),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=256), nullable=False),
        sa.Column("display_name", sa.String(length=256)),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("memory_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=128),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column("event_id", sa.String(length=36), sa.ForeignKey("events.id")),
        sa.Column("job_type", sa.String(length=128), nullable=False),
        sa.Column("status", job_status, nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("run_after", sa.DateTime(timezone=True)),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("jobs")
    op.drop_table("entities")
    op.drop_table("events")
    op.drop_table("memories_index")
    op.drop_table("categories")
    op.drop_table("api_keys")
    op.drop_table("projects")

    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        event_status.drop(bind, checkfirst=True)
        job_status.drop(bind, checkfirst=True)
