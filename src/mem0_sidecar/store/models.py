from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class EventStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ExportStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    default_user_id: Mapped[str | None] = mapped_column(String(256))
    default_app_id: Mapped[str | None] = mapped_column(String(256))
    default_agent_id: Mapped[str | None] = mapped_column(String(256))
    mem0_base_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    mem0_api_key_ref: Mapped[str | None] = mapped_column(String(256))
    settings_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    scopes_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "name",
            name="uq_categories_project_id_name",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    schema_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    strategy: Mapped[str] = mapped_column(
        String(64), default="metadata", nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class MemoryIndex(Base):
    __tablename__ = "memories_index"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "mem0_memory_id",
            name="uq_memories_index_project_mem0_memory_id",
        ),
        Index(
            "ix_memories_index_project_active_created",
            "project_id",
            "deleted_at",
            "created_at",
        ),
        Index(
            "ix_memories_index_project_app_user",
            "project_id",
            "app_id",
            "user_id",
        ),
        Index(
            "ix_memories_index_project_app_agent",
            "project_id",
            "app_id",
            "agent_id",
        ),
        Index(
            "ix_memories_index_project_app_run",
            "project_id",
            "app_id",
            "run_id",
        ),
        Index(
            "ix_memories_index_project_category",
            "project_id",
            "category",
        ),
        Index(
            "ix_memories_index_consolidation_exact",
            "project_id",
            "app_id",
            "normalized_type",
            "content_hash",
        ),
        Index(
            "ix_memories_index_consolidation_dirty",
            "project_id",
            "app_id",
            "consolidation_state",
            "last_observed_at",
            "last_consolidation_scan_at",
        ),
        Index(
            "ix_memories_index_scope_marker_backfill",
            "project_id",
            "app_id",
            "scope_markers_verified",
            "scope_marker_backfill_attempted_at",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    mem0_memory_id: Mapped[str] = mapped_column(String(256), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(256))
    agent_id: Mapped[str | None] = mapped_column(String(256))
    app_id: Mapped[str | None] = mapped_column(String(256))
    run_id: Mapped[str | None] = mapped_column(String(256))
    category: Mapped[str | None] = mapped_column(String(128))
    entity_refs_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    metadata_projection_json: Mapped[str] = mapped_column(
        Text, default="{}", nullable=False
    )
    content_hash: Mapped[str | None] = mapped_column(String(64))
    content_length: Mapped[int | None] = mapped_column(Integer)
    normalized_type: Mapped[str | None] = mapped_column(String(128))
    source: Mapped[str | None] = mapped_column(String(128))
    pinned: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_consolidation_scan_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    scope_markers_verified: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    scope_marker_backfill_status: Mapped[str] = mapped_column(
        String(32), default="PENDING", server_default=text("'PENDING'"), nullable=False
    )
    scope_marker_backfill_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    consolidation_state: Mapped[str] = mapped_column(
        String(32),
        default="ACTIVE",
        server_default=text("'ACTIVE'"),
        nullable=False,
    )
    shadowed_by_proposal_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_project_created", "project_id", "created_at"),
        Index(
            "ix_events_project_app_created",
            "project_id",
            "app_id",
            "created_at",
        ),
        Index(
            "ix_events_project_app_user_created",
            "project_id",
            "app_id",
            "user_id",
            "created_at",
        ),
        Index(
            "ix_events_project_app_agent_created",
            "project_id",
            "app_id",
            "agent_id",
            "created_at",
        ),
        Index(
            "ix_events_project_app_run_created",
            "project_id",
            "app_id",
            "run_id",
            "created_at",
        ),
        Index(
            "ix_events_project_operation_created",
            "project_id",
            "operation",
            "created_at",
        ),
        Index(
            "ix_events_project_status_created",
            "project_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_events_project_has_results_created",
            "project_id",
            "has_results",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    app_id: Mapped[str | None] = mapped_column(String(256))
    user_id: Mapped[str | None] = mapped_column(String(256))
    agent_id: Mapped[str | None] = mapped_column(String(256))
    run_id: Mapped[str | None] = mapped_column(String(256))
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[EventStatus] = mapped_column(
        SAEnum(EventStatus), default=EventStatus.PENDING, nullable=False
    )
    subject_type: Mapped[str | None] = mapped_column(String(128))
    subject_id: Mapped[str | None] = mapped_column(String(256))
    request_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    response_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    error_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(256))
    latency_ms: Mapped[float | None] = mapped_column(Float)
    result_count: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    has_results: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Entity(Base):
    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "app_id",
            "entity_type",
            "entity_id",
            name="uq_entities_project_app_type_id",
        ),
        Index(
            "ix_entities_project_app_type_updated",
            "project_id",
            "app_id",
            "entity_type",
            "updated_at",
        ),
        Index(
            "ix_entities_project_app_last_seen",
            "project_id",
            "app_id",
            "last_seen_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    app_id: Mapped[str] = mapped_column(String(256), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(256))
    metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    memory_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class MutationIntent(Base):
    __tablename__ = "mutation_intents"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "app_id",
            "operation",
            "operation_key",
            name="uq_mutation_intents_scope_operation_key",
        ),
        Index(
            "ix_mutation_intents_scope_status_created",
            "project_id",
            "app_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    app_id: Mapped[str] = mapped_column(String(256), nullable=False)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="ACTIVE", server_default="ACTIVE", nullable=False
    )
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    result_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    error_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MutationIntentTarget(Base):
    __tablename__ = "mutation_intent_targets"
    __table_args__ = (
        UniqueConstraint(
            "intent_id",
            "memory_id",
            name="uq_mutation_intent_targets_intent_memory",
        ),
        Index(
            "ix_mutation_intent_targets_intent_status_ordinal",
            "intent_id",
            "status",
            "ordinal",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    intent_id: Mapped[str] = mapped_column(
        ForeignKey("mutation_intents.id", ondelete="CASCADE"), nullable=False
    )
    memory_id: Mapped[str] = mapped_column(String(256), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="PENDING", server_default="PENDING", nullable=False
    )
    error_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ConsolidationPolicy(Base):
    __tablename__ = "consolidation_policies"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "app_id",
            name="uq_consolidation_policy_scope",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id"), nullable=False
    )
    app_id: Mapped[str] = mapped_column(String(256), nullable=False)
    enabled: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    mode: Mapped[str] = mapped_column(
        String(32),
        default="OBSERVE",
        server_default=text("'OBSERVE'"),
        nullable=False,
    )
    policy_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default=text("'{}'"), nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ConsolidationRun(Base):
    __tablename__ = "consolidation_runs"
    __table_args__ = (
        Index(
            "ix_consolidation_runs_scope_status_created",
            "project_id",
            "app_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id"), nullable=False
    )
    app_id: Mapped[str] = mapped_column(String(256), nullable=False)
    policy_id: Mapped[str | None] = mapped_column(
        ForeignKey("consolidation_policies.id")
    )
    policy_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        default="PENDING",
        server_default=text("'PENDING'"),
        nullable=False,
    )
    scan_cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    counts_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default=text("'{}'"), nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default=text("'{}'"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConsolidationProposal(Base):
    __tablename__ = "consolidation_proposals"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "proposal_key",
            name="uq_consolidation_proposals_run_key",
        ),
        Index(
            "ix_consolidation_proposals_scope_status",
            "project_id",
            "app_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("consolidation_runs.id"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id"), nullable=False
    )
    app_id: Mapped[str] = mapped_column(String(256), nullable=False)
    proposal_key: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        default="PENDING",
        server_default=text("'PENDING'"),
        nullable=False,
    )
    source_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_memory_id: Mapped[str | None] = mapped_column(String(256))
    score: Mapped[float | None] = mapped_column(Float)
    evidence_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default=text("'{}'"), nullable=False
    )
    expected_hashes_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default=text("'{}'"), nullable=False
    )
    canonical_content_hash: Mapped[str | None] = mapped_column(String(64))
    export_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("export_jobs.id")
    )
    shadow_attempt_id: Mapped[str | None] = mapped_column(String(36))
    shadow_attempt_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConsolidationLineage(Base):
    __tablename__ = "consolidation_lineage"
    __table_args__ = (
        UniqueConstraint(
            "source_memory_id",
            "proposal_id",
            name="uq_consolidation_lineage_source_proposal",
        ),
        Index(
            "ix_consolidation_lineage_scope_applied",
            "project_id",
            "app_id",
            "applied_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id"), nullable=False
    )
    app_id: Mapped[str] = mapped_column(String(256), nullable=False)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("consolidation_runs.id"), nullable=False
    )
    proposal_id: Mapped[str] = mapped_column(
        ForeignKey("consolidation_proposals.id"), nullable=False
    )
    source_memory_id: Mapped[str] = mapped_column(String(256), nullable=False)
    canonical_memory_id: Mapped[str | None] = mapped_column(String(256))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    source_content_hash: Mapped[str | None] = mapped_column(String(64))
    export_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("export_jobs.id")
    )
    metadata_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default=text("'{}'"), nullable=False
    )
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "job_type",
            "dedupe_key",
            name="uq_jobs_project_type_dedupe",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    event_id: Mapped[str | None] = mapped_column(ForeignKey("events.id"))
    job_type: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        SAEnum(JobStatus), default=JobStatus.PENDING, nullable=False
    )
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    result_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default=text("'{}'"), nullable=False
    )
    dedupe_key: Mapped[str | None] = mapped_column(String(256))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    run_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ExportJob(Base):
    __tablename__ = "export_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    status: Mapped[ExportStatus] = mapped_column(
        SAEnum(ExportStatus),
        default=ExportStatus.PENDING,
        server_default=text("'PENDING'"),
        nullable=False,
    )
    format: Mapped[str] = mapped_column(String(32), nullable=False)
    filters_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default=text("'{}'"), nullable=False
    )
    result_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default=text("'{}'"), nullable=False
    )
    result_ref: Mapped[str | None] = mapped_column(String(1024))
    error_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default=text("'{}'"), nullable=False
    )
    total_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    exported_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    skipped_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
