import json
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import (
    BigInteger,
    Index,
    UniqueConstraint,
    create_engine,
    func,
    insert,
    select,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import mem0_sidecar.store.repositories as repositories
from mem0_sidecar.core.explorer_filters import (
    MEMORY_FILTER_FIELDS,
    parse_explorer_query,
)
from mem0_sidecar.store.models import (
    Base,
    Category,
    Entity,
    Event,
    EventStatus,
    ExportStatus,
    JobStatus,
    MemoryIndex,
    Project,
)
from mem0_sidecar.store.repositories import (
    CategoryRepository,
    EntityRepository,
    EventRepository,
    ExportJobRepository,
    JobRepository,
    MemoryIndexRepository,
    ProjectRepository,
)


def test_category_model_enforces_unique_name_per_project(db_session):
    constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in Category.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert constraints["uq_categories_project_id_name"] == ("project_id", "name")

    projects = ProjectRepository(db_session)
    projects.upsert_default_project(
        project_id="alpha", name="alpha", mem0_base_url="http://mem0:8000"
    )
    projects.upsert_default_project(
        project_id="beta", name="beta", mem0_base_url="http://mem0:8000"
    )
    db_session.add_all(
        [
            Category(project_id="alpha", name="work"),
            Category(project_id="beta", name="work"),
        ]
    )
    db_session.flush()
    db_session.add(Category(project_id="alpha", name="work"))

    with pytest.raises(IntegrityError):
        db_session.flush()


def test_project_mutation_lock_is_postgresql_row_lock(db_session, monkeypatch) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="alpha",
        name="alpha",
        mem0_base_url="http://mem0:8000",
    )
    statements: list[str] = []
    original_scalar = db_session.scalar

    def capture(statement, *args, **kwargs):
        statements.append(
            str(statement.compile(dialect=postgresql.dialect())).upper()
        )
        return original_scalar(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "scalar", capture)
    monkeypatch.setattr(db_session.get_bind().dialect, "name", "postgresql")

    project = ProjectRepository(db_session).lock_for_mutation("alpha")

    assert project.id == "alpha"
    assert any(" FOR UPDATE" in statement for statement in statements)


def test_category_repository_item_lifecycle_is_project_scoped(db_session):
    projects = ProjectRepository(db_session)
    projects.upsert_default_project(
        project_id="alpha", name="alpha", mem0_base_url="http://mem0:8000"
    )
    projects.upsert_default_project(
        project_id="beta", name="beta", mem0_base_url="http://mem0:8000"
    )
    repository = CategoryRepository(db_session)

    created = repository.create_project_category(
        project_id="alpha",
        item={
            "name": "preferences",
            "description": "Durable preferences",
            "schema": {"type": "object"},
            "enabled": True,
            "strategy": "metadata",
        },
    )
    db_session.commit()

    assert repository.get_project_category("alpha", created.id).name == "preferences"
    assert repository.find_project_category_by_name("alpha", "preferences") is not None
    assert repository.find_project_category_by_name("beta", "preferences") is None

    updated = repository.update_project_category(
        "alpha", created.id, {"description": "Updated", "enabled": False}
    )
    db_session.commit()
    assert updated.description == "Updated"
    assert updated.enabled == 0
    assert updated.version == 2

    repository.delete_project_category("alpha", created.id)
    db_session.commit()
    with pytest.raises(KeyError):
        repository.get_project_category("alpha", created.id)


def test_category_repository_replaces_category_with_same_name(db_session):
    projects = ProjectRepository(db_session)
    projects.upsert_default_project(
        project_id="alpha", name="alpha", mem0_base_url="http://mem0:8000"
    )
    repository = CategoryRepository(db_session)
    original = repository.create_project_category(
        project_id="alpha",
        item={"name": "work", "description": "Before", "schema": {}},
    )
    original_id = original.id
    db_session.commit()

    replacements = repository.replace_project_categories(
        project_id="alpha",
        categories=[{"name": "work", "description": "After", "schema": {}}],
    )
    db_session.commit()

    assert len(replacements) == 1
    assert replacements[0].id != original_id
    assert replacements[0].description == "After"
    remaining_ids = [
        category.id for category in repository.list_project_categories("alpha")
    ]
    assert remaining_ids == [replacements[0].id]


def test_repositories_support_control_plane_flow(db_session) -> None:
    project_repo = ProjectRepository(db_session)
    category_repo = CategoryRepository(db_session)
    event_repo = EventRepository(db_session)
    memory_repo = MemoryIndexRepository(db_session)
    entity_repo = EntityRepository(db_session)
    job_repo = JobRepository(db_session)

    project = project_repo.upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
        default_user_id="root",
        default_agent_id="codex",
    )
    categories = category_repo.replace_project_categories(
        project_id=project.id,
        categories=[{"name": "decision", "description": "Architecture decisions"}],
    )
    event = event_repo.create_event(
        project_id=project.id,
        operation="memory.add",
        allow_project_scope=True,
    )
    memory = memory_repo.upsert_memory(
        project_id=project.id,
        mem0_memory_id="mem-1",
        user_id="root",
        app_id="repo-a",
        category="decision",
        metadata={"type": "decision"},
    )
    entity = entity_repo.upsert_entity(
        project_id=project.id,
        entity_type="app",
        entity_id="repo-a",
        display_name="Repo A",
    )
    job = job_repo.enqueue(
        project_id=project.id,
        event_id=event.id,
        job_type="entity.rebuild",
        payload={},
    )
    event_repo.mark_succeeded(event.id, response={"memory_id": memory.mem0_memory_id})
    db_session.commit()

    assert categories[0].name == "decision"
    assert event_repo.get(event.id).status is EventStatus.SUCCEEDED
    assert memory.category == "decision"
    assert entity.memory_count == 0
    assert job.status is JobStatus.PENDING
    assert job_repo.claim_next().id == job.id


def _entity_projection_signature(entities: list[Entity]) -> list[tuple[object, ...]]:
    return [
        (
            entity.project_id,
            entity.app_id,
            entity.entity_type,
            entity.entity_id,
            entity.memory_count,
            entity.last_seen_at,
        )
        for entity in entities
    ]


def test_entity_model_has_app_scoped_identity_and_ordered_indexes(db_session) -> None:
    constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in Entity.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in Entity.__table__.indexes
    }

    assert Entity.__table__.c.app_id.nullable is False
    assert constraints["uq_entities_project_app_type_id"] == (
        "project_id",
        "app_id",
        "entity_type",
        "entity_id",
    )
    assert indexes["ix_entities_project_app_type_updated"] == (
        "project_id",
        "app_id",
        "entity_type",
        "updated_at",
    )
    assert indexes["ix_entities_project_app_last_seen"] == (
        "project_id",
        "app_id",
        "last_seen_at",
    )


def test_entity_rebuild_is_scoped_complete_and_idempotent(db_session) -> None:
    projects = ProjectRepository(db_session)
    for project_id in ("alpha", "beta"):
        projects.upsert_default_project(
            project_id=project_id,
            name=project_id,
            mem0_base_url="http://mem0:8000",
        )
    older = datetime(2026, 7, 12, 12, tzinfo=UTC)
    newer = datetime(2026, 7, 13, 12, tzinfo=UTC)
    deleted = datetime(2026, 7, 13, 13, tzinfo=UTC)
    db_session.add_all(
        [
            MemoryIndex(
                project_id="alpha",
                mem0_memory_id="alpha-a-old",
                app_id="app-a",
                user_id="alice",
                agent_id="agent-1",
                run_id="run-1",
                updated_at=older,
            ),
            MemoryIndex(
                project_id="alpha",
                mem0_memory_id="alpha-a-new",
                app_id="app-a",
                user_id="alice",
                agent_id=None,
                run_id="run-2",
                updated_at=newer,
            ),
            MemoryIndex(
                project_id="alpha",
                mem0_memory_id="alpha-a-deleted",
                app_id="app-a",
                user_id="deleted-user",
                agent_id="deleted-agent",
                run_id="deleted-run",
                updated_at=deleted,
                deleted_at=deleted,
            ),
            MemoryIndex(
                project_id="alpha",
                mem0_memory_id="alpha-b",
                app_id="app-b",
                user_id="alice",
                agent_id="agent-b",
                run_id="run-b",
                updated_at=deleted,
            ),
            MemoryIndex(
                project_id="beta",
                mem0_memory_id="beta-a",
                app_id="app-a",
                user_id="alice",
                agent_id="agent-beta",
                run_id="run-beta",
                updated_at=deleted,
            ),
        ]
    )
    db_session.add_all(
        [
            Entity(
                project_id="alpha",
                app_id="app-a",
                entity_type="user",
                entity_id="stale",
                memory_count=99,
            ),
            Entity(
                project_id="alpha",
                app_id="app-b",
                entity_type="user",
                entity_id="preserved-app",
                memory_count=99,
            ),
            Entity(
                project_id="beta",
                app_id="app-a",
                entity_type="user",
                entity_id="preserved-project",
                memory_count=99,
            ),
        ]
    )
    db_session.flush()
    repository = EntityRepository(db_session)

    first = repository.rebuild_project_entities("alpha", "app-a")
    first_signature = _entity_projection_signature(first)
    second = repository.rebuild_project_entities("alpha", "app-a")

    assert first_signature == [
        ("alpha", "app-a", "agent", "agent-1", 1, older),
        ("alpha", "app-a", "app", "app-a", 2, newer),
        ("alpha", "app-a", "run", "run-1", 1, older),
        ("alpha", "app-a", "run", "run-2", 1, newer),
        ("alpha", "app-a", "user", "alice", 2, newer),
    ]
    assert _entity_projection_signature(second) == first_signature
    assert db_session.scalar(
        select(func.count())
        .select_from(Entity)
        .where(Entity.project_id == "alpha", Entity.app_id == "app-a")
    ) == len(first_signature)
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(Entity)
            .where(Entity.project_id == "alpha", Entity.app_id == "app-b")
        )
        == 1
    )
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(Entity)
            .where(Entity.project_id == "beta", Entity.app_id == "app-a")
        )
        == 1
    )


def test_entity_refresh_updates_only_requested_identities_at_high_cardinality(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="alpha",
        name="alpha",
        mem0_base_url="http://mem0:8000",
        default_app_id="app-a",
    )
    now = datetime(2026, 7, 17, 12, tzinfo=UTC)
    db_session.add_all(
        [
            MemoryIndex(
                project_id="alpha",
                app_id="app-a",
                mem0_memory_id=f"unrelated-{index:04d}",
                user_id=f"unrelated-user-{index:04d}",
                updated_at=now,
            )
            for index in range(1000)
        ]
        + [
            MemoryIndex(
                project_id="alpha",
                app_id="app-a",
                mem0_memory_id="target-old",
                user_id="target-user",
                updated_at=now - timedelta(minutes=1),
            ),
            MemoryIndex(
                project_id="alpha",
                app_id="app-a",
                mem0_memory_id="target-new",
                user_id="target-user",
                updated_at=now,
            ),
        ]
    )
    db_session.add_all(
        [
            Entity(
                project_id="alpha",
                app_id="app-a",
                entity_type="user",
                entity_id="target-user",
                memory_count=99,
            ),
            Entity(
                project_id="alpha",
                app_id="app-a",
                entity_type="user",
                entity_id="gone-user",
                memory_count=99,
            ),
            Entity(
                project_id="alpha",
                app_id="app-a",
                entity_type="user",
                entity_id="preserved-sentinel",
                memory_count=7,
            ),
        ]
    )
    db_session.flush()

    refreshed = EntityRepository(db_session).refresh_affected_entities(
        "alpha",
        "app-a",
        {("user", "target-user"), ("user", "gone-user")},
    )

    assert [
        (item.entity_id, item.memory_count, item.last_seen_at) for item in refreshed
    ] == [("target-user", 2, now)]
    assert set(
        db_session.execute(
            select(Entity.entity_id, Entity.memory_count).where(
                Entity.project_id == "alpha",
                Entity.app_id == "app-a",
            )
        )
    ) == {("target-user", 2), ("preserved-sentinel", 7)}


def test_entity_detail_and_memory_ids_are_strictly_scoped_and_ordered(
    db_session,
) -> None:
    projects = ProjectRepository(db_session)
    for project_id in ("alpha", "beta"):
        projects.upsert_default_project(
            project_id=project_id,
            name=project_id,
            mem0_base_url="http://mem0:8000",
        )
    newest = datetime(2026, 7, 13, 12, tzinfo=UTC)
    older = newest - timedelta(days=1)
    db_session.add_all(
        [
            MemoryIndex(
                project_id="alpha",
                mem0_memory_id="memory-b",
                app_id="app-a",
                user_id="alice",
                agent_id="agent-1",
                run_id="run-1",
                updated_at=newest,
            ),
            MemoryIndex(
                project_id="alpha",
                mem0_memory_id="memory-a",
                app_id="app-a",
                user_id="alice",
                agent_id="agent-2",
                run_id="run-1",
                updated_at=newest,
            ),
            MemoryIndex(
                project_id="alpha",
                mem0_memory_id="memory-old",
                app_id="app-a",
                user_id="alice",
                agent_id="agent-1",
                run_id="run-2",
                updated_at=older,
            ),
            MemoryIndex(
                project_id="alpha",
                mem0_memory_id="memory-deleted",
                app_id="app-a",
                user_id="alice",
                updated_at=newest,
                deleted_at=newest,
            ),
            MemoryIndex(
                project_id="alpha",
                mem0_memory_id="memory-other-app",
                app_id="app-b",
                user_id="alice",
                updated_at=newest,
            ),
            MemoryIndex(
                project_id="beta",
                mem0_memory_id="memory-other-project",
                app_id="app-a",
                user_id="alice",
                updated_at=newest,
            ),
        ]
    )
    db_session.flush()
    repository = EntityRepository(db_session)
    repository.rebuild_project_entities("alpha", "app-a")

    entity = repository.get_project_entity("alpha", "app-a", "user", "alice")
    assert entity.memory_count == 3
    assert repository.list_entity_memory_ids("alpha", "app-a", "user", "alice") == [
        "memory-a",
        "memory-b",
        "memory-old",
    ]
    assert repository.list_entity_memory_ids(
        "alpha", "app-a", "agent", "agent-1"
    ) == ["memory-b", "memory-old"]
    assert repository.list_entity_memory_ids(
        "alpha", "app-a", "agent", "agent-2"
    ) == ["memory-a"]
    assert repository.list_entity_memory_ids(
        "alpha", "app-a", "app", "app-a"
    ) == ["memory-a", "memory-b", "memory-old"]
    assert repository.list_entity_memory_ids(
        "alpha", "app-a", "run", "run-1"
    ) == ["memory-a", "memory-b"]
    assert repository.list_entity_memory_ids(
        "alpha", "app-a", "run", "run-2"
    ) == ["memory-old"]
    with pytest.raises(KeyError):
        repository.get_project_entity("alpha", "app-b", "user", "alice")
    with pytest.raises(KeyError):
        repository.get_project_entity("beta", "app-a", "user", "alice")

    for entity_type in ("session", "USER", ""):
        with pytest.raises(ValueError, match="^Unsupported entity type$"):
            repository.get_project_entity("alpha", "app-a", entity_type, "alice")
        with pytest.raises(ValueError, match="^Unsupported entity type$"):
            repository.list_entity_memory_ids("alpha", "app-a", entity_type, "alice")


@pytest.mark.parametrize(
    "entity_type",
    [None, [], {}, "unknown"],
)
def test_entity_detail_and_memory_ids_reject_hostile_entity_types(
    db_session,
    entity_type,
) -> None:
    repository = EntityRepository(db_session)

    with pytest.raises(ValueError, match="^Unsupported entity type$"):
        repository.get_project_entity("alpha", "app-a", entity_type, "alice")
    with pytest.raises(ValueError, match="^Unsupported entity type$"):
        repository.list_entity_memory_ids(
            "alpha", "app-a", entity_type, "alice"
        )


def test_entity_detail_and_memory_ids_reject_string_subclass(db_session) -> None:
    class EntityTypeSubclass(str):
        pass

    repository = EntityRepository(db_session)
    entity_type = EntityTypeSubclass("user")

    with pytest.raises(ValueError, match="^Unsupported entity type$"):
        repository.get_project_entity("alpha", "app-a", entity_type, "alice")
    with pytest.raises(ValueError, match="^Unsupported entity type$"):
        repository.list_entity_memory_ids(
            "alpha", "app-a", entity_type, "alice"
        )


def test_entity_memory_ids_reject_unbounded_mutation_targets(
    db_session,
    monkeypatch,
) -> None:
    repository = EntityRepository(db_session)
    memory_ids = [f"memory-{index}" for index in range(5001)]
    monkeypatch.setattr(db_session, "scalars", lambda statement: iter(memory_ids))

    with pytest.raises(
        ValueError,
        match="mutation intent exceeds 5000 memory targets",
    ):
        repository.list_entity_memory_ids("alpha", "app-a", "user", "alice")


def test_entity_rebuild_locks_project_before_snapshot_and_delete(
    db_session,
    monkeypatch,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="alpha",
        name="alpha",
        mem0_base_url="http://mem0:8000",
    )
    db_session.add(
        MemoryIndex(
            project_id="alpha",
            mem0_memory_id="memory-1",
            app_id="app-a",
            user_id="alice",
        )
    )
    db_session.flush()
    project = db_session.get(Project, "alpha")
    assert project is not None
    original_scalars = db_session.scalars
    original_execute = db_session.execute
    operations: list[str] = []

    def record_project_lock(repository, project_id):
        operations.append("project_lock")
        return project

    def record_memory_snapshot(statement, *args, **kwargs):
        operations.append("memory_snapshot")
        return original_scalars(statement, *args, **kwargs)

    def record_scope_delete(statement, *args, **kwargs):
        operations.append("scope_delete")
        return original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(ProjectRepository, "lock_for_mutation", record_project_lock)
    monkeypatch.setattr(db_session, "scalars", record_memory_snapshot)
    monkeypatch.setattr(db_session, "execute", record_scope_delete)

    EntityRepository(db_session).rebuild_project_entities("alpha", "app-a")

    assert operations == ["project_lock", "memory_snapshot", "scope_delete"]


def test_entity_rebuild_rejects_scope_beyond_memory_scan_limit_before_delete(
    db_session,
    monkeypatch,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="alpha",
        name="alpha",
        mem0_base_url="http://mem0:8000",
    )
    project = db_session.get(Project, "alpha")
    assert project is not None
    snapshot = [
        SimpleNamespace(
            user_id=f"user-{index}",
            agent_id=None,
            app_id="app-a",
            run_id=None,
            updated_at=datetime(2026, 7, 17, tzinfo=UTC),
        )
        for index in range(5001)
    ]
    monkeypatch.setattr(
        ProjectRepository,
        "lock_for_mutation",
        lambda repository, project_id: project,
    )
    def bounded_snapshot(statement):
        rendered = str(statement.compile(compile_kwargs={"literal_binds": True}))
        assert "LIMIT 5001" in rendered
        return iter(snapshot)

    monkeypatch.setattr(db_session, "scalars", bounded_snapshot)

    def reject_delete(*args, **kwargs):
        raise AssertionError("entity scope delete must not run after an oversized scan")

    monkeypatch.setattr(db_session, "execute", reject_delete)

    with pytest.raises(
        ValueError,
        match="entity rebuild exceeds 5000 active memories",
    ):
        EntityRepository(db_session).rebuild_project_entities("alpha", "app-a")


def test_entity_rebuild_rejects_unknown_project_before_snapshot_or_delete(
    db_session,
    monkeypatch,
) -> None:
    operations: list[str] = []

    def record_project_lock(repository, project_id):
        operations.append("project_lock")
        raise KeyError(project_id)

    monkeypatch.setattr(ProjectRepository, "lock_for_mutation", record_project_lock)
    monkeypatch.setattr(
        db_session,
        "scalars",
        lambda *args, **kwargs: operations.append("memory_snapshot"),
    )
    monkeypatch.setattr(
        db_session,
        "execute",
        lambda *args, **kwargs: operations.append("scope_delete"),
    )

    with pytest.raises(KeyError, match="missing-project"):
        EntityRepository(db_session).rebuild_project_entities(
            "missing-project", "app-a"
        )

    assert operations == ["project_lock"]


def test_entity_rebuild_uses_one_select_one_delete_and_one_flush(
    db_session,
    monkeypatch,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="alpha",
        name="alpha",
        mem0_base_url="http://mem0:8000",
    )
    db_session.add(
        MemoryIndex(
            project_id="alpha",
            mem0_memory_id="memory-1",
            app_id="app-a",
            user_id="alice",
        )
    )
    db_session.flush()
    project = db_session.get(Project, "alpha")
    assert project is not None
    original_scalars = db_session.scalars
    original_execute = db_session.execute
    original_flush = db_session.flush
    calls = {"select": 0, "delete": 0, "flush": 0}

    def counted_scalars(statement, *args, **kwargs):
        calls["select"] += 1
        return original_scalars(statement, *args, **kwargs)

    def counted_execute(statement, *args, **kwargs):
        calls["delete"] += 1
        return original_execute(statement, *args, **kwargs)

    def counted_flush(*args, **kwargs):
        calls["flush"] += 1
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(db_session, "scalars", counted_scalars)
    monkeypatch.setattr(db_session, "execute", counted_execute)
    monkeypatch.setattr(db_session, "flush", counted_flush)
    monkeypatch.setattr(
        ProjectRepository,
        "lock_for_mutation",
        lambda repository, project_id: project,
    )

    EntityRepository(db_session).rebuild_project_entities("alpha", "app-a")

    assert calls == {"select": 1, "delete": 1, "flush": 1}


def test_memory_index_repository_isolates_same_mem0_id_per_project(db_session) -> None:
    project_repo = ProjectRepository(db_session)
    memory_repo = MemoryIndexRepository(db_session)

    project_repo.upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    project_repo.upsert_default_project(
        project_id="repo-b",
        name="Repo B",
        mem0_base_url="http://mem0:8000",
    )

    memory_a = memory_repo.upsert_memory(
        project_id="repo-a",
        mem0_memory_id="mem-shared",
        user_id="alice",
        app_id="repo-a",
        category="decision",
        metadata={"project": "repo-a"},
    )
    memory_b = memory_repo.upsert_memory(
        project_id="repo-b",
        mem0_memory_id="mem-shared",
        user_id="bob",
        app_id="repo-b",
        category="incident",
        metadata={"project": "repo-b"},
    )
    db_session.commit()

    assert memory_a.id != memory_b.id
    assert memory_a.project_id == "repo-a"
    assert memory_a.user_id == "alice"
    assert memory_a.category == "decision"
    assert memory_a.metadata_projection_json == '{"project": "repo-a"}'
    assert memory_b.project_id == "repo-b"
    assert memory_b.user_id == "bob"
    assert memory_b.category == "incident"
    assert memory_b.metadata_projection_json == '{"project": "repo-b"}'


def test_memory_index_repository_marks_memory_deleted(db_session) -> None:
    project_repo = ProjectRepository(db_session)
    memory_repo = MemoryIndexRepository(db_session)

    project_repo.upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    memory_repo.upsert_memory(
        project_id="repo-a",
        mem0_memory_id="mem-1",
        user_id="alice",
        app_id="repo-a",
        category="decision",
        metadata={},
    )

    deleted = memory_repo.delete_memory(
        project_id="repo-a",
        mem0_memory_id="mem-1",
    )
    db_session.commit()

    assert deleted is not None
    assert deleted.deleted_at is not None


def test_memory_index_repository_get_memory_scopes_by_project(db_session) -> None:
    project_repo = ProjectRepository(db_session)
    memory_repo = MemoryIndexRepository(db_session)

    project_repo.upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    project_repo.upsert_default_project(
        project_id="repo-b",
        name="Repo B",
        mem0_base_url="http://mem0:8000",
    )
    memory_repo.upsert_memory(
        project_id="repo-b",
        mem0_memory_id="mem-shared",
        user_id="bob",
        app_id="repo-b",
        category="incident",
        metadata={"project": "repo-b"},
    )

    assert memory_repo.get_memory(
        project_id="repo-a",
        mem0_memory_id="mem-shared",
    ) is None
    memory = memory_repo.get_memory(
        project_id="repo-b",
        mem0_memory_id="mem-shared",
    )

    assert memory is not None
    assert memory.user_id == "bob"
    assert memory.app_id == "repo-b"


def test_memory_index_repository_get_memory_ignores_deleted_by_default(
    db_session,
) -> None:
    project_repo = ProjectRepository(db_session)
    memory_repo = MemoryIndexRepository(db_session)

    project_repo.upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    memory_repo.upsert_memory(
        project_id="repo-a",
        mem0_memory_id="mem-1",
        user_id="alice",
        app_id="repo-a",
        category="decision",
        metadata={"project": "repo-a"},
    )
    memory_repo.delete_memory(
        project_id="repo-a",
        mem0_memory_id="mem-1",
    )

    assert (
        memory_repo.get_memory(project_id="repo-a", mem0_memory_id="mem-1")
        is None
    )


def test_memory_index_repository_get_memory_can_include_deleted_rows(
    db_session,
) -> None:
    project_repo = ProjectRepository(db_session)
    memory_repo = MemoryIndexRepository(db_session)

    project_repo.upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    memory_repo.upsert_memory(
        project_id="repo-a",
        mem0_memory_id="mem-1",
        user_id="alice",
        app_id="repo-a",
        category="decision",
        metadata={"project": "repo-a"},
    )
    memory_repo.delete_memory(
        project_id="repo-a",
        mem0_memory_id="mem-1",
    )

    memory = memory_repo.get_memory(
        project_id="repo-a",
        mem0_memory_id="mem-1",
        include_deleted=True,
    )

    assert memory is not None
    assert memory.deleted_at is not None


def test_memory_index_repository_lists_dirty_anchors_by_scope_and_cursor(
    db_session,
) -> None:
    projects = ProjectRepository(db_session)
    projects.upsert_default_project(
        project_id="repo-a", name="repo-a", mem0_base_url="http://mem0:8000"
    )
    projects.upsert_default_project(
        project_id="repo-b", name="repo-b", mem0_base_url="http://mem0:8000"
    )
    repository = MemoryIndexRepository(db_session)
    observed = datetime(2026, 7, 23, 1, tzinfo=UTC)
    for memory_id in ("mem-a", "mem-b", "mem-c"):
        repository.upsert_memory(
            project_id="repo-a",
            mem0_memory_id=memory_id,
            user_id="root",
            app_id="app-a",
            category="decision",
            content_hash=f"hash-{memory_id}",
            content_length=10,
            normalized_type="decision",
            source="manual",
            pinned=memory_id == "mem-c",
            observed_at=observed,
        )
    repository.upsert_memory(
        project_id="repo-b",
        mem0_memory_id="foreign",
        user_id="root",
        app_id="app-a",
        category="decision",
        content_hash="hash-foreign",
        content_length=10,
        normalized_type="decision",
        source="manual",
        pinned=False,
        observed_at=observed,
    )
    db_session.commit()

    first = repository.list_dirty_anchors(
        project_id="repo-a", app_id="app-a", limit=1
    )
    second = repository.list_dirty_anchors(
        project_id="repo-a",
        app_id="app-a",
        after_last_observed_at=first[0].last_observed_at,
        after_memory_id=first[0].mem0_memory_id,
        limit=10,
    )

    assert [memory.mem0_memory_id for memory in first + second] == [
        "mem-a",
        "mem-b",
    ]


def test_memory_index_changed_hash_reactivates_shadow_but_same_hash_does_not(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a", name="repo-a", mem0_base_url="http://mem0:8000"
    )
    repository = MemoryIndexRepository(db_session)
    observed = datetime(2026, 7, 23, 1, tzinfo=UTC)
    memory = repository.upsert_memory(
        project_id="repo-a",
        mem0_memory_id="mem-a",
        user_id="root",
        app_id="app-a",
        category="decision",
        content_hash="same",
        content_length=4,
        normalized_type="decision",
        source="manual",
        pinned=False,
        observed_at=observed,
    )
    memory.consolidation_state = "SHADOWED"
    memory.shadowed_by_proposal_id = "proposal-a"
    db_session.flush()

    unchanged = repository.upsert_memory(
        project_id="repo-a",
        mem0_memory_id="mem-a",
        user_id="root",
        app_id="app-a",
        category="decision",
        content_hash="same",
        content_length=4,
        normalized_type="decision",
        source="manual",
        pinned=False,
        observed_at=observed + timedelta(minutes=1),
    )
    assert unchanged.consolidation_state == "SHADOWED"

    changed = repository.upsert_memory(
        project_id="repo-a",
        mem0_memory_id="mem-a",
        user_id="root",
        app_id="app-a",
        category="decision",
        content_hash="changed",
        content_length=7,
        normalized_type="decision",
        source="manual",
        pinned=False,
        observed_at=observed + timedelta(minutes=2),
    )
    assert changed.consolidation_state == "ACTIVE"
    assert changed.shadowed_by_proposal_id is None


def test_export_job_repository_lifecycle(db_session):
    ProjectRepository(db_session).upsert_default_project(
        project_id="default",
        name="default",
        mem0_base_url="http://mem0:8000",
    )
    repo = ExportJobRepository(db_session)

    job = repo.create(
        project_id="default",
        export_format="json",
        filters={"app_id": "codex"},
    )
    assert job.status == ExportStatus.PENDING
    assert json.loads(job.filters_json) == {"app_id": "codex"}

    repo.mark_running("default", job.id)
    db_session.commit()
    db_session.expire_all()
    running = repo.get("default", job.id)
    assert running.status == ExportStatus.RUNNING
    assert running.started_at is not None

    repo.mark_succeeded(
        "default",
        job.id,
        result={"memories": [], "skipped": []},
        total_count=1,
        exported_count=0,
        skipped_count=1,
    )
    db_session.commit()
    db_session.expire_all()
    succeeded = repo.get("default", job.id)
    assert succeeded.status == ExportStatus.SUCCEEDED
    assert json.loads(succeeded.result_json) == {"memories": [], "skipped": []}
    assert succeeded.total_count == 1
    assert succeeded.exported_count == 0
    assert succeeded.skipped_count == 1
    assert succeeded.completed_at is not None


def test_export_job_repository_lifecycle_is_project_scoped(db_session) -> None:
    project_repo = ProjectRepository(db_session)
    for project_id in ("default", "other"):
        project_repo.upsert_default_project(
            project_id=project_id,
            name=project_id,
            mem0_base_url="http://mem0:8000",
        )

    repo = ExportJobRepository(db_session)
    job = repo.create(project_id="default", export_format="json", filters={})

    with pytest.raises(KeyError):
        repo.mark_running("other", job.id)

    repo.mark_running("default", job.id)

    with pytest.raises(KeyError):
        repo.mark_succeeded(
            "other",
            job.id,
            result={"memories": []},
            total_count=1,
            exported_count=1,
            skipped_count=0,
        )

    with pytest.raises(KeyError):
        repo.mark_failed("other", job.id, error={"message": "boom"})


def test_export_job_repository_failed_lifecycle_and_listing_reload_from_database(
    db_session,
) -> None:
    project_repo = ProjectRepository(db_session)
    project_repo.upsert_default_project(
        project_id="default",
        name="default",
        mem0_base_url="http://mem0:8000",
    )
    project_repo.upsert_default_project(
        project_id="other",
        name="other",
        mem0_base_url="http://mem0:8000",
    )
    repo = ExportJobRepository(db_session)

    older = repo.create(
        project_id="default",
        export_format="json",
        filters={"app_id": "older"},
    )
    failed = repo.create(
        project_id="default",
        export_format="csv",
        filters={"app_id": "failed"},
    )
    repo.create(
        project_id="other",
        export_format="json",
        filters={"app_id": "hidden"},
    )
    db_session.commit()

    repo.mark_failed("default", failed.id, error={"message": "boom"})
    db_session.commit()
    db_session.expire_all()

    reloaded = repo.get("default", failed.id)
    assert reloaded.status == ExportStatus.FAILED
    assert json.loads(reloaded.error_json) == {"message": "boom"}
    assert reloaded.completed_at is not None

    listed = repo.list_project_exports("default")
    assert [job.id for job in listed] == [failed.id, older.id]


def test_export_job_repository_get_is_project_scoped(db_session):
    for project_id in ("default", "other"):
        ProjectRepository(db_session).upsert_default_project(
            project_id=project_id,
            name=project_id,
            mem0_base_url="http://mem0:8000",
        )

    repo = ExportJobRepository(db_session)
    job = repo.create(project_id="default", export_format="json", filters={})

    assert repo.get("default", job.id).id == job.id
    try:
        repo.get("other", job.id)
    except KeyError as exc:
        assert str(exc).strip("'") == job.id
    else:
        raise AssertionError("Expected project-scoped lookup to fail")


def test_memory_index_repository_lists_export_candidates(db_session):
    ProjectRepository(db_session).upsert_default_project(
        project_id="default",
        name="default",
        mem0_base_url="http://mem0:8000",
    )
    repo = MemoryIndexRepository(db_session)
    repo.upsert_memory(
        project_id="default",
        mem0_memory_id="mem-a",
        user_id="root",
        app_id="codex",
        agent_id=None,
        run_id=None,
        category="preferences",
        metadata={"source": "test"},
    )
    repo.upsert_memory(
        project_id="default",
        mem0_memory_id="mem-b",
        user_id="other",
        app_id="codex",
        agent_id=None,
        run_id=None,
        category=None,
        metadata={},
    )

    candidates = repo.list_export_candidates(
        project_id="default",
        filters={"user_id": "root", "app_id": "codex"},
    )

    assert [item.mem0_memory_id for item in candidates] == ["mem-a"]


def _explorer_query(**overrides):
    payload = {"page_size": 100, **overrides}
    return parse_explorer_query(payload, allowed_fields=MEMORY_FILTER_FIELDS)


def _add_explorer_memory(
    db_session,
    *,
    project_id: str,
    mem0_memory_id: str,
    app_id: str = "app-a",
    user_id: str | None = None,
    agent_id: str | None = None,
    run_id: str | None = None,
    category: str | None = None,
    metadata: dict[str, object] | None = None,
    created_at: datetime | None = None,
    deleted_at: datetime | None = None,
) -> MemoryIndex:
    memory = MemoryIndex(
        project_id=project_id,
        mem0_memory_id=mem0_memory_id,
        app_id=app_id,
        user_id=user_id,
        agent_id=agent_id,
        run_id=run_id,
        category=category,
        metadata_projection_json=json.dumps(metadata or {}, sort_keys=True),
        created_at=created_at or datetime(2026, 7, 1, tzinfo=UTC),
        deleted_at=deleted_at,
    )
    db_session.add(memory)
    return memory


def test_memory_explorer_scope_is_outer_to_all_and_any_filters(db_session):
    projects = ProjectRepository(db_session)
    for project_id in ("alpha", "beta"):
        projects.upsert_default_project(
            project_id=project_id,
            name=project_id,
            mem0_base_url="http://mem0:8000",
        )

    _add_explorer_memory(
        db_session,
        project_id="alpha",
        mem0_memory_id="mem-all",
        user_id="alice",
        category="work",
    )
    _add_explorer_memory(
        db_session,
        project_id="alpha",
        mem0_memory_id="mem-user",
        user_id="alice",
        category="personal",
    )
    _add_explorer_memory(
        db_session,
        project_id="alpha",
        mem0_memory_id="mem-category",
        user_id="bob",
        category="work",
    )
    _add_explorer_memory(
        db_session,
        project_id="alpha",
        app_id="app-b",
        mem0_memory_id="mem-other-app",
        user_id="alice",
        category="work",
    )
    _add_explorer_memory(
        db_session,
        project_id="beta",
        mem0_memory_id="mem-other-project",
        user_id="alice",
        category="work",
    )
    _add_explorer_memory(
        db_session,
        project_id="alpha",
        mem0_memory_id="mem-deleted",
        user_id="alice",
        category="work",
        deleted_at=datetime(2026, 7, 2, tzinfo=UTC),
    )
    db_session.flush()

    filters = [
        {"field": "user_id", "operator": "equals", "value": "alice"},
        {"field": "category", "operator": "equals", "value": "work"},
    ]
    repository = MemoryIndexRepository(db_session)

    all_page = repository.query_project_memories(
        "alpha", "app-a", _explorer_query(match="all", filters=filters)
    )
    any_page = repository.query_project_memories(
        "alpha", "app-a", _explorer_query(match="any", filters=filters)
    )

    assert [item.mem0_memory_id for item in all_page.items] == ["mem-all"]
    assert all_page.total == 1
    assert all_page.scan_count == 0
    assert {item.mem0_memory_id for item in any_page.items} == {
        "mem-all",
        "mem-user",
        "mem-category",
    }
    assert any_page.total == 3
    with pytest.raises(FrozenInstanceError):
        all_page.total = 2


@pytest.mark.parametrize(
    ("entity_type", "expected_ids"),
    [
        ("user", {"mem-user", "mem-mixed"}),
        ("agent", {"mem-agent", "mem-mixed"}),
        ("app", {"mem-user", "mem-agent", "mem-run", "mem-mixed"}),
        ("run", {"mem-run"}),
    ],
)
def test_memory_explorer_entity_type_matches_identity_column_presence(
    db_session, entity_type, expected_ids
):
    ProjectRepository(db_session).upsert_default_project(
        project_id="alpha",
        name="alpha",
        mem0_base_url="http://mem0:8000",
    )
    _add_explorer_memory(
        db_session,
        project_id="alpha",
        mem0_memory_id="mem-user",
        user_id="alice",
    )
    _add_explorer_memory(
        db_session,
        project_id="alpha",
        mem0_memory_id="mem-agent",
        agent_id="codex",
    )
    _add_explorer_memory(
        db_session,
        project_id="alpha",
        mem0_memory_id="mem-run",
        run_id="run-1",
    )
    _add_explorer_memory(
        db_session,
        project_id="alpha",
        mem0_memory_id="mem-mixed",
        user_id="alice",
        agent_id="codex",
    )
    db_session.flush()

    page = MemoryIndexRepository(db_session).query_project_memories(
        "alpha",
        "app-a",
        _explorer_query(
            filters=[
                {
                    "field": "entity_type",
                    "operator": "equals",
                    "value": entity_type,
                }
            ]
        ),
    )

    assert {item.mem0_memory_id for item in page.items} == expected_ids


def test_memory_explorer_date_range_is_inclusive_and_paging_is_stable(db_session):
    ProjectRepository(db_session).upsert_default_project(
        project_id="alpha",
        name="alpha",
        mem0_base_url="http://mem0:8000",
    )
    start = datetime(2026, 7, 10, 12, tzinfo=UTC)
    end = start + timedelta(days=1)
    for memory_id, created_at in (
        ("mem-before", start - timedelta(microseconds=1)),
        ("mem-b", start),
        ("mem-a", start),
        ("mem-c", start),
        ("mem-end", end),
        ("mem-after", end + timedelta(microseconds=1)),
    ):
        _add_explorer_memory(
            db_session,
            project_id="alpha",
            mem0_memory_id=memory_id,
            created_at=created_at,
        )
    db_session.flush()

    repository = MemoryIndexRepository(db_session)
    date_range = {"from": start.isoformat(), "to": end.isoformat()}
    first_page = repository.query_project_memories(
        "alpha",
        "app-a",
        _explorer_query(
            date_range=date_range,
            sort="created_at_asc",
            page=1,
            page_size=2,
        ),
    )
    second_page = repository.query_project_memories(
        "alpha",
        "app-a",
        _explorer_query(
            date_range=date_range,
            sort="created_at_asc",
            page=2,
            page_size=2,
        ),
    )
    descending = repository.query_project_memories(
        "alpha",
        "app-a",
        _explorer_query(date_range=date_range, sort="created_at_desc"),
    )

    assert [item.mem0_memory_id for item in first_page.items] == ["mem-a", "mem-b"]
    assert [item.mem0_memory_id for item in second_page.items] == [
        "mem-c",
        "mem-end",
    ]
    assert first_page.total == second_page.total == 4
    assert [item.mem0_memory_id for item in descending.items] == [
        "mem-end",
        "mem-c",
        "mem-b",
        "mem-a",
    ]


def test_memory_explorer_metadata_exact_match_uses_safe_scalar_narrowing(db_session):
    ProjectRepository(db_session).upsert_default_project(
        project_id="alpha",
        name="alpha",
        mem0_base_url="http://mem0:8000",
    )
    _add_explorer_memory(
        db_session,
        project_id="alpha",
        mem0_memory_id="mem-both",
        user_id="alice",
        metadata={"source": "codex"},
    )
    _add_explorer_memory(
        db_session,
        project_id="alpha",
        mem0_memory_id="mem-scalar",
        user_id="alice",
        metadata={"source": "chatgpt"},
    )
    malformed = _add_explorer_memory(
        db_session,
        project_id="alpha",
        mem0_memory_id="mem-malformed",
        user_id="alice",
    )
    malformed.metadata_projection_json = "{not-json"
    _add_explorer_memory(
        db_session,
        project_id="alpha",
        mem0_memory_id="mem-metadata",
        user_id="bob",
        metadata={"source": "codex"},
    )
    db_session.flush()

    filters = [
        {"field": "user_id", "operator": "equals", "value": "alice"},
        {
            "field": "metadata",
            "operator": "contains",
            "value": {"key": "source", "value": "codex"},
        },
    ]
    repository = MemoryIndexRepository(db_session)
    all_page = repository.query_project_memories(
        "alpha", "app-a", _explorer_query(match="all", filters=filters)
    )
    any_page = repository.query_project_memories(
        "alpha", "app-a", _explorer_query(match="any", filters=filters)
    )

    assert [item.mem0_memory_id for item in all_page.items] == ["mem-both"]
    assert all_page.total == 1
    assert all_page.scan_count == 3
    assert {item.mem0_memory_id for item in any_page.items} == {
        "mem-both",
        "mem-scalar",
        "mem-malformed",
        "mem-metadata",
    }
    assert any_page.total == 4
    assert any_page.scan_count == 4


def test_memory_explorer_rejects_metadata_scans_over_5000_before_loading(
    db_session,
):
    ProjectRepository(db_session).upsert_default_project(
        project_id="alpha",
        name="alpha",
        mem0_base_url="http://mem0:8000",
    )
    created_at = datetime(2026, 7, 1, tzinfo=UTC)
    db_session.execute(
        insert(MemoryIndex),
        [
            {
                "project_id": "alpha",
                "mem0_memory_id": f"mem-{index:04d}",
                "app_id": "app-a",
                "user_id": "alice" if index == 0 else "bob",
                "metadata_projection_json": '{"source": "codex"}',
                "created_at": created_at,
            }
            for index in range(5001)
        ],
    )
    repository = MemoryIndexRepository(db_session)
    metadata_filter = {
        "field": "metadata",
        "operator": "contains",
        "value": {"key": "source", "value": "codex"},
    }

    narrowed = repository.query_project_memories(
        "alpha",
        "app-a",
        _explorer_query(
            match="all",
            filters=[
                {"field": "user_id", "operator": "equals", "value": "alice"},
                metadata_filter,
            ],
        ),
    )

    assert [item.mem0_memory_id for item in narrowed.items] == ["mem-0000"]
    assert narrowed.scan_count == 1
    with pytest.raises(
        ValueError, match="^metadata filter scan exceeds 5000 records$"
    ):
        repository.query_project_memories(
            "alpha",
            "app-a",
            _explorer_query(filters=[metadata_filter]),
        )


def test_memory_explorer_rechecks_metadata_scan_bound_after_count_race(
    db_session,
    monkeypatch,
):
    ProjectRepository(db_session).upsert_default_project(
        project_id="alpha",
        name="alpha",
        mem0_base_url="http://mem0:8000",
    )
    created_at = datetime(2026, 7, 1, tzinfo=UTC)
    db_session.execute(
        insert(MemoryIndex),
        [
            {
                "project_id": "alpha",
                "mem0_memory_id": f"mem-{index:04d}",
                "app_id": "app-a",
                "metadata_projection_json": '{"source":"codex"}',
                "created_at": created_at,
            }
            for index in range(5001)
        ],
    )
    repository = MemoryIndexRepository(db_session)
    metadata_filter = {
        "field": "metadata",
        "operator": "contains",
        "value": {"key": "source", "value": "codex"},
    }

    # Simulate a concurrent insert committed after COUNT but before SELECT.
    monkeypatch.setattr(db_session, "scalar", lambda *args, **kwargs: 5000)

    with pytest.raises(
        ValueError, match="^metadata filter scan exceeds 5000 records$"
    ):
        repository.query_project_memories(
            "alpha",
            "app-a",
            _explorer_query(filters=[metadata_filter]),
        )


def test_memory_index_repository_mark_stale_never_crosses_projects(db_session):
    projects = ProjectRepository(db_session)
    for project_id in ("alpha", "beta"):
        projects.upsert_default_project(
            project_id=project_id,
            name=project_id,
            mem0_base_url="http://mem0:8000",
        )
    alpha_shared = _add_explorer_memory(
        db_session,
        project_id="alpha",
        mem0_memory_id="mem-shared",
    )
    beta_shared = _add_explorer_memory(
        db_session,
        project_id="beta",
        mem0_memory_id="mem-shared",
    )
    beta_only = _add_explorer_memory(
        db_session,
        project_id="beta",
        mem0_memory_id="mem-beta-only",
    )
    old_deleted_at = datetime(2026, 7, 1, tzinfo=UTC)
    alpha_deleted = _add_explorer_memory(
        db_session,
        project_id="alpha",
        mem0_memory_id="mem-deleted",
        deleted_at=old_deleted_at,
    )
    db_session.flush()

    changed = MemoryIndexRepository(db_session).mark_stale(
        "alpha",
        ["mem-shared", "mem-shared", "mem-beta-only", "mem-deleted"],
    )

    assert changed == 1
    assert alpha_shared.deleted_at is not None
    assert beta_shared.deleted_at is None
    assert beta_only.deleted_at is None
    assert alpha_deleted.deleted_at == old_deleted_at


def test_memory_index_repository_stale_compare_and_set_is_scoped_and_cutoff_safe(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="alpha",
        name="alpha",
        mem0_base_url="http://mem0:8000",
    )
    cutoff = datetime(2026, 7, 2, tzinfo=UTC)
    before_cutoff = datetime(2026, 7, 1, tzinfo=UTC)
    after_cutoff = datetime(2026, 7, 3, tzinfo=UTC)
    eligible = _add_explorer_memory(
        db_session,
        project_id="alpha",
        mem0_memory_id="eligible",
        app_id="app-a",
    )
    updated_during_scan = _add_explorer_memory(
        db_session,
        project_id="alpha",
        mem0_memory_id="updated",
        app_id="app-a",
    )
    other_app = _add_explorer_memory(
        db_session,
        project_id="alpha",
        mem0_memory_id="other-app",
        app_id="app-b",
    )
    eligible.updated_at = before_cutoff
    updated_during_scan.updated_at = after_cutoff
    other_app.updated_at = before_cutoff
    db_session.flush()

    changed = MemoryIndexRepository(db_session).mark_stale_if_unchanged(
        project_id="alpha",
        app_id="app-a",
        mem0_memory_ids=["eligible", "updated", "other-app"],
        updated_at_lte=cutoff,
    )

    assert changed == 1
    assert eligible.deleted_at is not None
    assert updated_during_scan.deleted_at is None
    assert other_app.deleted_at is None


def test_memory_index_repository_identical_upsert_touches_timestamp_and_beats_cutoff(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="alpha",
        name="alpha",
        mem0_base_url="http://mem0:8000",
    )
    repository = MemoryIndexRepository(db_session)
    memory = repository.upsert_memory(
        project_id="alpha",
        mem0_memory_id="same-values",
        user_id="alice",
        agent_id="codex",
        app_id="app-a",
        run_id="run-1",
        category="note",
        metadata={"type": "note"},
    )
    memory.updated_at = datetime(2026, 7, 1, tzinfo=UTC)
    db_session.flush()
    cutoff = memory.updated_at

    refreshed = repository.upsert_memory(
        project_id="alpha",
        mem0_memory_id="same-values",
        user_id="alice",
        agent_id="codex",
        app_id="app-a",
        run_id="run-1",
        category="note",
        metadata={"type": "note"},
    )

    assert refreshed.updated_at > cutoff
    changed = repository.mark_stale_if_unchanged(
        project_id="alpha",
        app_id="app-a",
        mem0_memory_ids=["same-values"],
        updated_at_lte=cutoff,
    )
    assert changed == 0
    assert refreshed.deleted_at is None


def test_memory_index_repository_claims_missing_or_same_app_but_rejects_other_app(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="alpha",
        name="alpha",
        mem0_base_url="http://mem0:8000",
    )
    repository = MemoryIndexRepository(db_session)

    created = repository.claim_memory(
        project_id="alpha",
        mem0_memory_id="claimable",
        user_id="alice",
        agent_id=None,
        app_id="app-a",
        run_id=None,
        category=None,
        metadata={"version": 1},
    )
    same_app = repository.claim_memory(
        project_id="alpha",
        mem0_memory_id="claimable",
        user_id="alice",
        agent_id=None,
        app_id="app-a",
        run_id=None,
        category="note",
        metadata={"version": 2},
    )
    other_app = repository.claim_memory(
        project_id="alpha",
        mem0_memory_id="claimable",
        user_id="mallory",
        agent_id=None,
        app_id="app-b",
        run_id=None,
        category="stolen",
        metadata={"version": 3},
    )

    assert created.status == "claimed"
    assert same_app.status == "claimed"
    assert other_app.status == "conflict"
    projection = repository.get_memory(
        project_id="alpha",
        mem0_memory_id="claimable",
    )
    assert projection is not None
    assert projection.app_id == "app-a"
    assert projection.category == "note"
    assert projection.metadata_projection_json == '{"version": 2}'


def test_memory_index_repository_concurrent_claim_has_exactly_one_app_winner(
    tmp_path,
) -> None:
    database_path = tmp_path / "claim-race.sqlite3"
    engine = create_engine(
        f"sqlite+pysqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as setup_session:
        ProjectRepository(setup_session).upsert_default_project(
            project_id="alpha",
            name="alpha",
            mem0_base_url="http://mem0:8000",
        )
        setup_session.commit()

    barrier = Barrier(2)

    def claim(app_id: str) -> str:
        with Session(engine) as session:
            barrier.wait()
            result = MemoryIndexRepository(session).claim_memory(
                project_id="alpha",
                mem0_memory_id="raced",
                user_id="alice",
                agent_id=None,
                app_id=app_id,
                run_id=None,
                category=None,
                metadata={},
            )
            session.commit()
            return result.status

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(claim, ("app-a", "app-b")))

    assert sorted(statuses) == ["claimed", "conflict"]
    with Session(engine) as verification_session:
        projection = MemoryIndexRepository(verification_session).get_memory(
            project_id="alpha",
            mem0_memory_id="raced",
        )
        assert projection is not None
        assert projection.app_id in {"app-a", "app-b"}


def test_project_repository_preserves_existing_defaults_on_routed_upsert(
    db_session,
) -> None:
    project_repo = ProjectRepository(db_session)

    original = project_repo.upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
        default_user_id="root",
        default_agent_id="codex",
    )
    project_repo.upsert_default_project(
        project_id="repo-a",
        name="Repo A v2",
        mem0_base_url="http://mem0:8000",
    )
    db_session.commit()

    project = db_session.get(type(original), "repo-a")

    assert project is not None
    assert project.default_user_id == "root"
    assert project.default_agent_id == "codex"


def test_project_repository_can_preserve_existing_default_app_id(db_session) -> None:
    project_repo = ProjectRepository(db_session)

    original = project_repo.upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
        default_app_id="app-x",
    )
    project_repo.upsert_default_project(
        project_id="repo-a",
        name="Repo A v2",
        mem0_base_url="http://mem0:8000",
    )
    db_session.commit()

    project = db_session.get(type(original), "repo-a")

    assert project is not None
    assert project.default_app_id == "app-x"


def test_project_repository_can_update_default_app_id_explicitly(db_session) -> None:
    project_repo = ProjectRepository(db_session)

    project_repo.upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
        default_app_id="app-x",
    )
    project_repo.upsert_default_project(
        project_id="repo-a",
        name="Repo A v2",
        mem0_base_url="http://mem0:8000",
        default_app_id="app-y",
    )
    db_session.commit()

    project = db_session.get(Project, "repo-a")

    assert project is not None
    assert project.default_app_id == "app-y"


def test_event_repository_lists_and_gets_events_scoped_to_project(db_session) -> None:
    project_repo = ProjectRepository(db_session)
    event_repo = EventRepository(db_session)

    project_repo.upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    project_repo.upsert_default_project(
        project_id="repo-b",
        name="Repo B",
        mem0_base_url="http://mem0:8000",
    )

    visible = event_repo.create_event(
        project_id="repo-a",
        app_id="app-a",
        operation="memory.add",
        request={"app_id": "app-a", "text": "visible"},
        subject_type="memory",
        subject_id="mem-a",
    )
    event_repo.mark_succeeded(visible.id, response={"id": "mem-a"})

    hidden = event_repo.create_event(
        project_id="repo-b",
        app_id="app-a",
        operation="memory.add",
        request={"app_id": "app-a", "text": "hidden"},
        subject_type="memory",
        subject_id="mem-b",
    )
    event_repo.mark_succeeded(hidden.id, response={"id": "mem-b"})
    wrong_app = event_repo.create_event(
        project_id="repo-a",
        app_id="app-b",
        operation="memory.add",
        request={"app_id": "app-b", "text": "wrong app"},
        subject_type="memory",
        subject_id="mem-c",
    )
    event_repo.mark_succeeded(wrong_app.id, response={"id": "mem-c"})
    db_session.commit()

    listed = event_repo.list_project_events("repo-a")
    fetched = event_repo.get_project_event("repo-a", "app-a", visible.id)

    assert [event.id for event in listed] == [visible.id, wrong_app.id]
    assert fetched.id == visible.id
    assert fetched.project_id == "repo-a"

    with pytest.raises(KeyError):
        event_repo.get_project_event("repo-a", "app-a", hidden.id)
    with pytest.raises(KeyError):
        event_repo.get_project_event("repo-a", "app-a", wrong_app.id)


def test_event_repository_legacy_list_allows_5000_and_rejects_5001(
    db_session,
) -> None:
    created_at = datetime(2026, 7, 13, tzinfo=UTC)
    rows = [
        {
            "id": f"event-{index:04d}",
            "project_id": "repo-a",
            "operation": "memory.search",
            "status": EventStatus.SUCCEEDED,
            "request_json": "{}",
            "response_json": "{}",
            "error_json": "{}",
            "result_count": 0,
            "has_results": 0,
            "created_at": created_at,
        }
        for index in range(5000)
    ]
    db_session.execute(insert(Event), rows)
    repository = EventRepository(db_session)

    assert len(repository.list_project_events("repo-a")) == 5000

    db_session.execute(
        insert(Event),
        {
            "id": "event-over-limit",
            "project_id": "repo-a",
            "operation": "memory.search",
            "status": EventStatus.SUCCEEDED,
            "request_json": "{}",
            "response_json": "{}",
            "error_json": "{}",
            "result_count": 0,
            "has_results": 0,
            "created_at": created_at,
        },
    )

    with pytest.raises(
        ValueError,
        match="event list exceeds 5000 records; use POST /v1/events/query",
    ):
        repository.list_project_events("repo-a")


def test_event_model_matches_request_trace_migration() -> None:
    columns = Event.__table__.columns
    indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in Event.__table__.indexes
        if isinstance(index, Index)
    }

    assert columns["correlation_id"].nullable is True
    for field_name in ("app_id", "user_id", "agent_id", "run_id"):
        assert columns[field_name].nullable is True
        assert columns[field_name].type.compile(
            dialect=postgresql.dialect()
        ) == "VARCHAR(256)"
    assert columns["latency_ms"].nullable is True
    assert columns["result_count"].nullable is False
    assert columns["has_results"].nullable is False
    assert columns["result_count"].default.arg == 0
    assert columns["has_results"].default.arg == 0
    assert str(columns["result_count"].server_default.arg) == "0"
    assert str(columns["has_results"].server_default.arg) == "0"
    assert isinstance(columns["result_count"].type, BigInteger)
    assert columns["result_count"].type.compile(dialect=postgresql.dialect()) == (
        "BIGINT"
    )
    assert columns["correlation_id"].type.compile(
        dialect=postgresql.dialect()
    ) == "VARCHAR(256)"
    assert indexes == {
        "ix_events_project_created": ("project_id", "created_at"),
        "ix_events_project_app_created": (
            "project_id",
            "app_id",
            "created_at",
        ),
        "ix_events_project_app_user_created": (
            "project_id",
            "app_id",
            "user_id",
            "created_at",
        ),
        "ix_events_project_app_agent_created": (
            "project_id",
            "app_id",
            "agent_id",
            "created_at",
        ),
        "ix_events_project_app_run_created": (
            "project_id",
            "app_id",
            "run_id",
            "created_at",
        ),
        "ix_events_project_operation_created": (
            "project_id",
            "operation",
            "created_at",
        ),
        "ix_events_project_status_created": (
            "project_id",
            "status",
            "created_at",
        ),
        "ix_events_project_has_results_created": (
            "project_id",
            "has_results",
            "created_at",
        ),
    }


def test_event_repository_sanitizes_requests_and_success_results(
    db_session,
    monkeypatch,
) -> None:
    projects = ProjectRepository(db_session)
    projects.upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="https://memory.example.com/api",
    )
    started_at = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    completed_at = started_at + timedelta(milliseconds=125)
    times = iter((started_at, completed_at))
    monkeypatch.setattr(repositories, "_utc_now", lambda: next(times))
    repository = EventRepository(db_session)

    event = repository.create_event(
        project_id="repo-a",
        operation="memory.search",
        correlation_id="request-123",
        request={
            "app_id": "app-a",
            "user_id": "alice",
            "query": "Where are my notes?",
            "api_key": "must-not-persist",
            "internal_url": "http://mem0-internal:8000/v1",
            "configured_url": "https://memory.example.com/v1",
            "public_url": "https://example.com/docs?topic=memory",
        },
    )

    assert event.started_at == started_at
    assert event.correlation_id == "request-123"
    assert json.loads(event.request_json) == {
        "api_key": "[REDACTED]",
        "app_id": "app-a",
        "configured_url": "[REDACTED_URL]",
        "internal_url": "[REDACTED_URL]",
        "public_url": "https://example.com/docs?topic=memory",
        "query": "Where are my notes?",
        "user_id": "alice",
    }

    succeeded = repository.mark_succeeded(
        event.id,
        response={
            "authorization": "Bearer must-not-persist",
            "results": [
                {
                    "id": "mem-1",
                    "memory": "first",
                    "user_id": "alice",
                    "api_key": "preview-secret",
                },
                {"id": "mem-2", "memory": "second", "score": 0.75},
            ],
            "total": 2,
            "message": (
                "See https://example.com/docs; upstream https://memory.example.com/v1."
            ),
            "upstream_url": "https://mem0-internal:8443/v1/search",
        },
    )
    response = json.loads(succeeded.response_json)

    assert succeeded.status is EventStatus.SUCCEEDED
    assert succeeded.completed_at == completed_at
    assert succeeded.latency_ms == pytest.approx(125.0)
    assert succeeded.result_count == 2
    assert succeeded.has_results == 1
    assert "authorization" not in response
    assert json.loads(succeeded.request_json)["internal_url"] == "[REDACTED_URL]"
    assert response["message"] == (
        "See https://example.com/docs; upstream [REDACTED_URL]."
    )
    assert "upstream_url" not in response
    assert "results" not in response
    assert response["result_previews"] == [
        {"id": "mem-1", "memory": "first", "user_id": "alice"},
        {"id": "mem-2", "memory": "second", "score": 0.75},
    ]
    assert len(succeeded.request_json.encode()) <= 65_536
    assert len(succeeded.response_json.encode()) <= 65_536
    assert "mem0-internal" not in succeeded.request_json
    assert "memory.example.com" not in succeeded.request_json
    assert "memory.example.com" not in succeeded.response_json


def test_event_repository_persists_only_strict_bounded_result_previews(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    repository = EventRepository(db_session)
    event = repository.create_event(
        project_id="repo-a",
        operation="memory.search",
        request={"app_id": "app-a"},
    )
    raw_results = [
        {
            "id": f"mem-{index}",
            "memory": f"memory-{index}",
            "metadata": {
                "private": f"private-payload-{index}",
                "nested": [f"nested-leak-{index}"] * 100,
            },
            "arbitrary": {"must_not_persist": index},
        }
        for index in range(100)
    ]

    succeeded = repository.mark_succeeded(
        event.id,
        response={
            "results": raw_results,
            "total": 100,
            "message": "ok",
            "safe_summary": "must-not-persist",
            "Results": [{"metadata": "case-variant-leak"}],
            "memories": [{"metadata": "memories-leak"}],
            "data": {"results": [{"metadata": "nested-results-leak"}]},
            "status": {"nested": "structured-status-leak"},
        },
    )
    stored = json.loads(succeeded.response_json)

    assert succeeded.result_count == 100
    assert succeeded.has_results == 1
    assert "results" not in stored
    assert stored["message"] == "ok"
    assert "safe_summary" not in stored
    assert "Results" not in stored
    assert "memories" not in stored
    assert "data" not in stored
    assert "status" not in stored
    assert stored["result_previews"] == [
        {"id": f"mem-{index}", "memory": f"memory-{index}"} for index in range(20)
    ]
    assert stored["result_previews_omitted"] == 80
    assert '"results"' not in succeeded.response_json
    assert "private-payload" not in succeeded.response_json
    assert "nested-leak" not in succeeded.response_json
    assert "must_not_persist" not in succeeded.response_json
    assert "case-variant-leak" not in succeeded.response_json
    assert "memories-leak" not in succeeded.response_json
    assert "nested-results-leak" not in succeeded.response_json
    assert "structured-status-leak" not in succeeded.response_json


def test_event_repository_preserves_trusted_signed_64_bit_result_count(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    repository = EventRepository(db_session)
    event = repository.create_event(
        project_id="repo-a",
        operation="memory.search",
        request={"app_id": "app-a"},
    )

    succeeded = repository.mark_succeeded(
        event.id,
        response={
            "results": [{"id": "mem-1", "memory": "one"}],
            "total": 2**63 - 1,
        },
    )

    assert succeeded.result_count == 2**63 - 1
    assert succeeded.has_results == 1
    assert "result_previews_omitted" not in json.loads(succeeded.response_json)


def test_event_preview_omission_uses_returned_items_not_trusted_total(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    repository = EventRepository(db_session)
    event = repository.create_event(
        project_id="repo-a",
        operation="memory.search",
        request={"app_id": "app-a"},
    )

    succeeded = repository.mark_succeeded(
        event.id,
        response={
            "results": [
                {"id": f"mem-{index}", "memory": "visible"} for index in range(10)
            ],
            "total": 1000,
        },
    )
    stored = json.loads(succeeded.response_json)

    assert succeeded.result_count == 1000
    assert len(stored["result_previews"]) == 10
    assert "result_previews_omitted" not in stored


def test_event_preview_omission_counts_all_unrepresented_returned_items(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    repository = EventRepository(db_session)
    event = repository.create_event(
        project_id="repo-a",
        operation="memory.search",
        request={"app_id": "app-a"},
    )
    response = {
        "results": [{"metadata": {"not": "previewable"}} for _index in range(50)]
        + [{"id": f"mem-{index}", "memory": "visible"} for index in range(20)]
    }

    succeeded = repository.mark_succeeded(event.id, response=response)
    stored = json.loads(succeeded.response_json)

    assert succeeded.result_count == 70
    assert len(stored["result_previews"]) == 20
    assert stored["result_previews_omitted"] == 50


@pytest.mark.parametrize("returned_count", [101, 150])
def test_event_preview_metadata_uses_actual_large_result_list_length(
    db_session,
    returned_count,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    repository = EventRepository(db_session)
    event = repository.create_event(
        project_id="repo-a",
        operation="memory.search",
        request={"app_id": "app-a"},
    )

    succeeded = repository.mark_succeeded(
        event.id,
        response={
            "results": [
                {"id": f"mem-{index}", "memory": "visible"}
                for index in range(returned_count)
            ]
        },
    )
    stored = json.loads(succeeded.response_json)

    assert len(stored["result_previews"]) == 20
    assert stored["result_previews_omitted"] == returned_count - 20
    assert stored["result_previews_scan_truncated"] is True


def test_event_response_envelope_extraction_has_bounded_mapping_work(
    db_session,
) -> None:
    class CountingMapping(Mapping[str, object]):
        def __init__(self, values: dict[str, object]) -> None:
            self.values = values
            self.value_reads = 0

        def __getitem__(self, key: str) -> object:
            self.value_reads += 1
            return self.values[key]

        def __iter__(self) -> Iterator[str]:
            return iter(self.values)

        def __len__(self) -> int:
            return len(self.values)

    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    repository = EventRepository(db_session)
    event = repository.create_event(
        project_id="repo-a",
        operation="memory.search",
        request={"app_id": "app-a"},
    )
    response = CountingMapping(
        {
            "results": [{"id": "mem-1", "memory": "one"}],
            "total": 1,
            "message": "ok",
            **{f"field_{index:05d}": index for index in range(10_000)},
        }
    )

    succeeded = repository.mark_succeeded(
        event.id,
        response=response,  # type: ignore[arg-type]
    )
    stored = json.loads(succeeded.response_json)

    assert succeeded.result_count == 1
    assert stored["result_previews"] == [{"id": "mem-1", "memory": "one"}]
    assert stored["_trace_response_envelope_truncated"] is True
    assert "message" not in stored
    assert '"results"' not in succeeded.response_json
    assert len(succeeded.response_json.encode()) <= 65_536
    assert response.value_reads <= 2


def test_event_response_envelope_is_deterministic_when_key_scan_is_incomplete(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    repository = EventRepository(db_session)
    events = [
        repository.create_event(
            project_id="repo-a",
            operation="memory.search",
            request={"app_id": "app-a"},
        )
        for _index in range(2)
    ]
    entries = [
        ("results", [{"id": "mem-1", "memory": "one"}]),
        ("message", "allowed-but-envelope-is-incomplete"),
        *((f"field_{index:03d}", f"value-{index}") for index in range(80)),
    ]

    forward = repository.mark_succeeded(events[0].id, response=dict(entries))
    reverse = repository.mark_succeeded(
        events[1].id,
        response=dict(reversed(entries)),
    )

    assert forward.response_json == reverse.response_json
    assert json.loads(forward.response_json) == {
        "_trace_response_envelope_truncated": True,
        "result_previews": [{"id": "mem-1", "memory": "one"}],
    }


def test_event_repository_sanitizes_failures_and_uses_created_latency_fallback(
    db_session,
    monkeypatch,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    created_at = datetime(2026, 7, 13, 11, 0, tzinfo=UTC)
    completed_at = created_at + timedelta(milliseconds=250)
    repository = EventRepository(db_session)
    event = repository.create_event(
        project_id="repo-a",
        operation="memory.search",
        request={"app_id": "app-a"},
    )
    event.created_at = created_at
    event.started_at = None
    event.result_count = 99
    event.has_results = 1
    monkeypatch.setattr(repositories, "_utc_now", lambda: completed_at)

    failed = repository.mark_failed(
        event.id,
        error={
            "token": "must-not-persist",
            "message": (
                "failed at https://user:pass@example.com/v1/search?token=secret), retry"
            ),
            "nested": [
                {"url": "http://10.0.0.1/v1;"},
                "configured http://mem0:8000/path?q=safe.",
                "public https://example.com/help?topic=memory.",
            ],
            "padding": "x" * 80_000,
        },
    )
    error = json.loads(failed.error_json)

    assert failed.status is EventStatus.FAILED
    assert failed.completed_at == completed_at
    assert failed.latency_ms == pytest.approx(250.0)
    assert failed.result_count == 0
    assert failed.has_results == 0
    assert error["token"] == "[REDACTED]"
    assert "[REDACTED_URL]" in failed.error_json
    assert "[REDACTED_URL]), retry" in error["message"]
    assert "user:pass" not in failed.error_json
    assert "token=secret" not in failed.error_json
    assert "10.0.0.1" not in failed.error_json
    assert "mem0:8000" not in failed.error_json
    assert "https://example.com/help?topic=memory." in failed.error_json
    assert len(failed.error_json.encode()) <= 65_536


def test_event_repository_bounds_or_rejects_hostile_correlation_ids(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    repository = EventRepository(db_session)
    long_id = "request-" + "x" * 500

    first = repository.create_event(
        project_id="repo-a",
        operation="memory.search",
        request={"app_id": "app-a"},
        correlation_id=long_id,
    )
    second = repository.create_event(
        project_id="repo-a",
        operation="memory.search",
        request={"app_id": "app-a"},
        correlation_id=long_id,
    )

    assert first.correlation_id == second.correlation_id
    assert first.correlation_id is not None
    assert first.correlation_id.startswith("[SHA256:")
    assert first.correlation_id.endswith("]")
    assert len(first.correlation_id) <= 256
    assert first.correlation_id != long_id
    for sensitive_id in (
        "https://user:pass@example.com/request",
        "https://example.com/request?token=secret",
        "http://mem0:8000/v1/request",
    ):
        sensitive = repository.create_event(
            project_id="repo-a",
            operation="memory.search",
            request={"app_id": "app-a"},
            correlation_id=sensitive_id,
        )
        assert sensitive.correlation_id is not None
        assert sensitive.correlation_id.startswith("[SHA256:")
        assert sensitive.correlation_id != sensitive_id
        assert len(sensitive.correlation_id) <= 256
    public_id = "https://example.com/request?attempt=1"
    public = repository.create_event(
        project_id="repo-a",
        operation="memory.search",
        request={"app_id": "app-a"},
        correlation_id=public_id,
    )
    assert public.correlation_id == public_id
    with pytest.raises(TypeError, match="correlation_id must be a string"):
        repository.create_event(
            project_id="repo-a",
            operation="memory.search",
            request={"app_id": "app-a"},
            correlation_id=object(),  # type: ignore[arg-type]
        )


def test_event_repository_persists_raw_canonical_app_scope_before_sanitizing(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    repository = EventRepository(db_session)
    max_length_app_id = "m" * 256
    cases = {
        "private-url": (
            "http://private.local/app",
            {"app_id": "http://private.local/app"},
        ),
        "wide-request": (
            "wide-scope",
            {
                **{f"field_{index:03d}": index for index in range(70)},
                "app_id": "wide-scope",
            },
        ),
        "sorted-out-request": (
            "sorted-out-scope",
            {
                **{f"00_field_{index:03d}": index for index in range(50)},
                "app_id": "sorted-out-scope",
            },
        ),
        "max-length": (max_length_app_id, {"app_id": max_length_app_id}),
    }

    events = {
        name: repository.create_event(
            project_id="repo-a",
            operation="memory.search",
            request=request,
        )
        for name, (_app_id, request) in cases.items()
    }
    db_session.flush()

    for name, (app_id, _request) in cases.items():
        event = events[name]
        assert event.app_id == app_id
        page = repository.query_project_events(
            "repo-a",
            app_id,
            repositories.EventQuery(
                entity_filters={"app_id": app_id},
                page=1,
                page_size=20,
            ),
        )
        assert [item.id for item in page.items] == [event.id]

    assert "private.local" not in events["private-url"].request_json
    assert "wide-scope" not in events["wide-request"].request_json
    assert "sorted-out-scope" not in events["sorted-out-request"].request_json
    assert repository.query_project_events(
        "repo-a",
        "[REDACTED_URL]",
        repositories.EventQuery(page=1, page_size=20),
    ).items == []
    assert repository.query_project_events(
        "repo-a",
        "wrong-app",
        repositories.EventQuery(page=1, page_size=20),
    ).items == []


def test_event_repository_validates_explicit_and_raw_canonical_app_scope(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    repository = EventRepository(db_session)

    explicit = repository.create_event(
        project_id="repo-a",
        app_id="app-a",
        operation="memory.update",
        request={"message": "no raw scope marker"},
    )
    assert explicit.app_id == "app-a"

    invalid_cases = [
        ({"app_id": "app-b"}, "app-a"),
        (
            {
                "app_id": "app-a",
                "metadata": {"_mem0_sidecar_app_id": "app-b"},
            },
            None,
        ),
        ({"app_id": ""}, None),
        ({"app_id": "x" * 257}, None),
        ({"app_id": 123}, "app-a"),
    ]
    for request, explicit_app_id in invalid_cases:
        with pytest.raises(ValueError, match="canonical event app scope"):
            repository.create_event(
                project_id="repo-a",
                app_id=explicit_app_id,
                operation="memory.search",
                request=request,
            )


def test_event_repository_scrubs_embedded_credentials_from_every_trace_field(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    repository = EventRepository(db_session)
    event = repository.create_event(
        project_id="repo-a",
        operation="memory.search",
        request={
            "app_id": "app-a",
            "message": (
                "Authorization: Bearer req-bearer; api_key=req-api, keep."
            ),
            "nested": [
                "password=nested-pass)",
                {"line": "x-api-key: line-secret\nkeep line"},
            ],
            "standalone": (
                "sk-request-secret ghp_abcdefghijklmnopqrstuvwxyz123456 "
                "xoxb-1234567890-secret AKIAABCDEFGHIJKLMNOP"
            ),
        },
        correlation_id="request sk-correlation-secret",
    )
    succeeded = repository.mark_succeeded(
        event.id,
        response={
            "message": "client_secret=resp-secret; public response.",
            "status": "token=status-secret, ok",
            "id": "sk-response-secret",
        },
    )
    failed = repository.create_event(
        project_id="repo-a",
        operation="memory.search",
        request={"app_id": "app-a"},
    )
    repository.mark_failed(
        failed.id,
        error={
            "message": "passphrase: error-pass. keep.",
            "nested": ["github_pat_error-secret", "xoxp-error-secret"],
        },
    )

    request_document = json.loads(event.request_json)
    response_document = json.loads(succeeded.response_json)
    error_document = json.loads(failed.error_json)
    assert request_document["message"] == "Authorization: [REDACTED]."
    assert request_document["nested"] == [
        "password=[REDACTED])",
        {"line": "x-api-key: [REDACTED]\nkeep line"},
    ]
    assert request_document["standalone"] == (
        "[REDACTED] [REDACTED] [REDACTED] [REDACTED]"
    )
    assert response_document == {
        "id": "[REDACTED]",
        "message": "client_secret=[REDACTED]; public response.",
        "status": "token=[REDACTED], ok",
    }
    assert error_document == {
        "message": "passphrase: [REDACTED]. keep.",
        "nested": ["[REDACTED]", "[REDACTED]"],
    }
    assert event.correlation_id is not None
    assert event.correlation_id.startswith("[SHA256:")
    persisted = event.request_json + succeeded.response_json + failed.error_json
    for secret in (
        "req-bearer",
        "req-api",
        "nested-pass",
        "line-secret",
        "sk-request-secret",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "xoxb-1234567890-secret",
        "AKIAABCDEFGHIJKLMNOP",
        "resp-secret",
        "status-secret",
        "sk-response-secret",
        "error-pass",
        "github_pat_error-secret",
        "xoxp-error-secret",
    ):
        assert secret not in persisted


def test_event_repository_scrubs_structured_and_full_authorization_values(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    repository = EventRepository(db_session)
    secrets = (
        "json-secret",
        "json-bearer",
        "multiword-secret",
        "private-secret",
        "prefix\\\"secret-suffix",
        "digest-nonce",
        "digest-response",
        "folded-secret",
        "aws-credential",
        "aws-signature",
        "proxy-secret",
        "correlation-secret",
    )
    event = repository.create_event(
        project_id="repo-a",
        app_id="app-a",
        operation="memory.search",
        request={
            "json": (
                '{"api_key":"json-secret",'
                '"Authorization":"Bearer json-bearer"}'
            ),
            "multiword": (
                "API key: multiword-secret; Private key: private-secret. keep"
            ),
            "escaped": 'api_key="prefix\\\"secret-suffix"; keep',
            "digest": (
                "Authorization: Digest username=user; nonce=digest-nonce; "
                "response=digest-response\n folded-secret\nkeep digest tail"
            ),
            "aws": (
                "Authorization: AWS4-HMAC-SHA256 Credential=aws-credential; "
                "Signature=aws-signature\nkeep aws tail"
            ),
            "embedded": (
                'prefix {"Proxy-Authorization":"Basic proxy-secret"} suffix'
            ),
        },
        correlation_id='{"api_key":"correlation-secret"}',
    )
    repository.mark_succeeded(
        event.id,
        response={
            "message": (
                '{"Authorization":"Digest nonce=digest-nonce, '
                'response=digest-response"}'
            ),
            "status": "Private key: private-secret",
            "id": 'api_key="prefix\\\"secret-suffix"',
        },
    )

    persisted = event.request_json + event.response_json
    for secret in secrets:
        assert secret not in persisted
    assert "keep digest tail" in event.request_json
    assert "keep aws tail" in event.request_json
    assert event.correlation_id is not None
    assert event.correlation_id.startswith("[SHA256:")


def test_event_repository_uses_shared_secret_vocabulary_for_url_components(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    repository = EventRepository(db_session)
    event = repository.create_event(
        project_id="repo-a",
        operation="memory.search",
        request={"app_id": "app-a"},
    )
    urls = [
        "https://example.com/?secret%5Fkey=query-secret",
        "https://example.com/?private_key=query-secret",
        "https://example.com/?code_verifier=query-secret",
        "https://example.com/?access_key_id=query-secret",
        "https://example.com/#access_token=fragment-secret&token_type=Bearer",
        "https://example.com/#access_token%3Dencoded-fragment-secret",
        "https://example.com/#access_token%253Ddouble-fragment-secret",
        "https://example.com/?secret%255Fkey=double-query-secret",
        "https://example.com/?broken",
        "https://example.com/?value=%ZZ",
    ]
    succeeded = repository.mark_succeeded(
        event.id,
        response={
            "message": " ".join(
                [
                    *urls,
                    "https://example.com/?topic=memory#overview",
                ]
            )
        },
    )
    stored_message = json.loads(succeeded.response_json)["message"]

    assert stored_message.count("[REDACTED_URL]") == len(urls)
    assert "query-secret" not in stored_message
    assert "fragment-secret" not in stored_message
    assert "https://example.com/?topic=memory#overview" in stored_message
    for url in urls[:5]:
        correlated = repository.create_event(
            project_id="repo-a",
            operation="memory.search",
            request={"app_id": "app-a"},
            correlation_id=url,
        )
        assert correlated.correlation_id is not None
        assert correlated.correlation_id.startswith("[SHA256:")


def _add_trace_event(
    db_session,
    *,
    event_id: str,
    project_id: str = "repo-a",
    app_id: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    run_id: str | None = None,
    operation: str = "memory.search",
    status: EventStatus = EventStatus.SUCCEEDED,
    request: object = None,
    created_at: datetime,
    result_count: int = 0,
) -> Event:
    event = Event(
        id=event_id,
        project_id=project_id,
        app_id=app_id,
        user_id=user_id,
        agent_id=agent_id,
        run_id=run_id,
        operation=operation,
        status=status,
        request_json=json.dumps(request if request is not None else {}),
        response_json="{}",
        error_json="{}",
        created_at=created_at,
        started_at=created_at,
        completed_at=created_at,
        result_count=result_count,
        has_results=1 if result_count else 0,
    )
    db_session.add(event)
    return event


def test_event_repository_persists_canonical_entity_filters_before_sanitizing(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    repository = EventRepository(db_session)
    request = {
        **{f"00_field_{index:03d}": index for index in range(70)},
        "app_id": "app-a",
        "user_id": "http://private.local/alice",
        "agent_id": "agent-a",
        "run_id": "run-a",
    }

    event = repository.create_event(
        project_id="repo-a",
        operation="memory.search",
        request=request,
    )
    db_session.flush()

    assert event.app_id == "app-a"
    assert event.user_id == "http://private.local/alice"
    assert event.agent_id == "agent-a"
    assert event.run_id == "run-a"
    assert "private.local" not in event.request_json
    exact = repository.query_project_events(
        "repo-a",
        "app-a",
        repositories.EventQuery(
            entity_filters={
                "user_id": "http://private.local/alice",
                "agent_id": "agent-a",
                "run_id": "run-a",
            },
            page=1,
            page_size=20,
        ),
    )
    assert [item.id for item in exact.items] == [event.id]
    marker = repository.query_project_events(
        "repo-a",
        "app-a",
        repositories.EventQuery(
            entity_filters={"user_id": "[REDACTED_URL]"},
            page=1,
            page_size=20,
        ),
    )
    assert marker.items == []


def test_event_repository_rejects_conflicting_canonical_entity_markers(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    repository = EventRepository(db_session)

    with pytest.raises(ValueError, match="canonical event user scope markers conflict"):
        repository.create_event(
            project_id="repo-a",
            app_id="app-a",
            user_id="alice",
            operation="memory.search",
            request={"user_id": "bob"},
        )


def test_event_repository_requires_explicit_project_scope_opt_in(db_session) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    repository = EventRepository(db_session)

    with pytest.raises(ValueError, match="canonical event app scope is required"):
        repository.create_event(
            project_id="repo-a",
            operation="control-plane.audit",
            request={"message": "project-only"},
        )

    event = repository.create_event(
        project_id="repo-a",
        operation="control-plane.audit",
        request={"message": "project-only"},
        allow_project_scope=True,
    )

    assert event.app_id is None


def test_event_query_filters_and_total_are_strictly_project_app_scoped(
    db_session,
) -> None:
    projects = ProjectRepository(db_session)
    for project_id in ("repo-a", "repo-b"):
        projects.upsert_default_project(
            project_id=project_id,
            name=project_id,
            mem0_base_url="http://mem0:8000",
        )
    base = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    _add_trace_event(
        db_session,
        event_id="visible-primary",
        request={
            "app_id": "app-a",
            "user_id": "alice",
            "agent_id": "codex",
            "run_id": "run-1",
        },
        created_at=base,
        result_count=2,
    )
    _add_trace_event(
        db_session,
        event_id="visible-sidecar-key",
        status=EventStatus.FAILED,
        request={"_mem0_sidecar_app_id": "app-a", "user_id": "alice"},
        created_at=base - timedelta(hours=1),
    )
    _add_trace_event(
        db_session,
        event_id="visible-legacy-metadata",
        request={
            "metadata": {"_mem0_sidecar_app_id": "app-a"},
            "user_id": "alice",
        },
        created_at=base - timedelta(hours=2),
    )
    _add_trace_event(
        db_session,
        event_id="visible-identical-markers",
        request={
            "app_id": "app-a",
            "_mem0_sidecar_app_id": "app-a",
            "metadata": {"_mem0_sidecar_app_id": "app-a"},
            "user_id": "alice",
        },
        created_at=base - timedelta(hours=3),
    )
    _add_trace_event(
        db_session,
        event_id="conflicting-top-level",
        request={
            "app_id": "app-b",
            "metadata": {"_mem0_sidecar_app_id": "app-a"},
            "user_id": "alice",
        },
        created_at=base,
    )
    _add_trace_event(
        db_session,
        event_id="wrong-app",
        request={"app_id": "app-b", "user_id": "alice"},
        created_at=base,
        result_count=3,
    )
    _add_trace_event(
        db_session,
        event_id="missing-app",
        request={"user_id": "alice"},
        created_at=base,
    )
    malformed = _add_trace_event(
        db_session,
        event_id="malformed-app",
        request={"app_id": 123, "user_id": "alice"},
        created_at=base,
    )
    malformed.request_json = "not-json"
    _add_trace_event(
        db_session,
        event_id="wrong-project",
        project_id="repo-b",
        request={"app_id": "app-a", "user_id": "alice"},
        created_at=base,
    )
    db_session.flush()
    repository = EventRepository(db_session)

    page = repository.query_project_events(
        "repo-a",
        "app-a",
        repositories.EventQuery(page=1, page_size=20),
    )

    assert [item.id for item in page.items] == [
        "visible-primary",
        "visible-sidecar-key",
        "visible-legacy-metadata",
        "visible-identical-markers",
    ]
    assert page.total == 4
    assert sum(bucket["count"] for bucket in page.buckets) == 4

    app_b = repository.query_project_events(
        "repo-a",
        "app-b",
        repositories.EventQuery(page=1, page_size=20),
    )
    assert "wrong-app" in [item.id for item in app_b.items]
    assert "conflicting-top-level" not in [item.id for item in app_b.items]

    filtered = repository.query_project_events(
        "repo-a",
        "app-a",
        repositories.EventQuery(
            operation="memory.search",
            statuses=(EventStatus.SUCCEEDED,),
            has_results=True,
            from_at=base - timedelta(minutes=1),
            to_at=base,
            entity_filters={
                "user_id": "alice",
                "agent_id": "codex",
                "run_id": "run-1",
            },
            page=1,
            page_size=20,
        ),
    )

    assert [item.id for item in filtered.items] == ["visible-primary"]
    assert filtered.total == 1


def test_event_query_omits_each_hostile_legacy_request_without_crashing(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    created_at = datetime(2026, 7, 13, tzinfo=UTC)
    _add_trace_event(
        db_session,
        event_id="visible",
        request={"app_id": "app-a"},
        created_at=created_at,
    )
    invalid_utf8 = _add_trace_event(
        db_session,
        event_id="invalid-utf8",
        request={"app_id": "app-a"},
        created_at=created_at,
    )
    invalid_utf8.request_json = b'{"app_id":"app-a"}\xff'  # type: ignore[assignment]
    huge_integer = _add_trace_event(
        db_session,
        event_id="huge-integer",
        request={"app_id": "app-a"},
        created_at=created_at,
    )
    huge_integer.request_json = '{"app_id":"app-a","value":' + "9" * 5000 + "}"
    deeply_nested = _add_trace_event(
        db_session,
        event_id="deeply-nested",
        request={"app_id": "app-a"},
        created_at=created_at,
    )
    deeply_nested.request_json = (
        '{"app_id":"app-a","value":' + "[" * 2000 + "0" + "]" * 2000 + "}"
    )
    oversized = _add_trace_event(
        db_session,
        event_id="oversized",
        request={"app_id": "app-a"},
        created_at=created_at,
    )
    oversized.request_json = '{"app_id":"app-a","padding":"' + "x" * 70_000 + '"}'
    db_session.flush()

    page = EventRepository(db_session).query_project_events(
        "repo-a",
        "app-a",
        repositories.EventQuery(page=1, page_size=20),
    )

    assert [item.id for item in page.items] == ["visible"]
    assert page.total == 1


def test_event_query_accepts_every_request_depth_written_by_create_event(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    nested: object = "leaf"
    for _index in range(8):
        nested = {"next": nested}
    repository = EventRepository(db_session)
    roundtrip = repository.create_event(
        project_id="repo-a",
        operation="memory.search",
        request={"app_id": "app-a", "nested": nested},
    )
    too_deep_nested: object = "leaf"
    for _index in range(9):
        too_deep_nested = {"next": too_deep_nested}
    too_deep = _add_trace_event(
        db_session,
        event_id="legacy-too-deep",
        request={"app_id": "app-a", "nested": too_deep_nested},
        created_at=datetime(2026, 7, 13, tzinfo=UTC),
    )
    db_session.flush()

    page = repository.query_project_events(
        "repo-a",
        "app-a",
        repositories.EventQuery(page=1, page_size=20),
    )

    assert roundtrip.id in [item.id for item in page.items]
    assert too_deep.id not in [item.id for item in page.items]


def test_event_query_normalizes_offset_date_bounds_to_utc(db_session) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    created_at = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    _add_trace_event(
        db_session,
        event_id="utc-noon",
        request={"app_id": "app-a"},
        created_at=created_at,
    )
    db_session.flush()
    same_instant = datetime(
        2026,
        7,
        13,
        14,
        0,
        tzinfo=timezone(timedelta(hours=2)),
    )

    page = EventRepository(db_session).query_project_events(
        "repo-a",
        "app-a",
        repositories.EventQuery(
            from_at=same_instant,
            to_at=same_instant,
            page=1,
            page_size=20,
        ),
    )

    assert [item.id for item in page.items] == ["utc-noon"]


def test_event_query_pages_newest_first_and_buckets_at_48_hour_boundary(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    first_hour = datetime(2026, 7, 1, 1, 30, tzinfo=UTC)
    second_hour = datetime(2026, 7, 1, 2, 15, tzinfo=UTC)
    for event_id, created_at in (
        ("event-a", first_hour),
        ("event-b", first_hour),
        ("event-c", second_hour),
    ):
        _add_trace_event(
            db_session,
            event_id=event_id,
            request={"app_id": "app-a"},
            created_at=created_at,
        )
    db_session.flush()
    repository = EventRepository(db_session)
    from_at = datetime(2026, 7, 1, tzinfo=UTC)
    exact_boundary = from_at + timedelta(hours=48)

    first_page = repository.query_project_events(
        "repo-a",
        "app-a",
        repositories.EventQuery(
            from_at=from_at,
            to_at=exact_boundary,
            page=1,
            page_size=2,
        ),
    )
    second_page = repository.query_project_events(
        "repo-a",
        "app-a",
        repositories.EventQuery(
            from_at=from_at,
            to_at=exact_boundary,
            page=2,
            page_size=2,
        ),
    )

    assert [item.id for item in first_page.items] == ["event-c", "event-b"]
    assert [item.id for item in second_page.items] == ["event-a"]
    assert first_page.total == second_page.total == 3
    assert first_page.buckets == [
        {"timestamp": "2026-07-01T01:00:00Z", "count": 2},
        {"timestamp": "2026-07-01T02:00:00Z", "count": 1},
    ]

    daily = repository.query_project_events(
        "repo-a",
        "app-a",
        repositories.EventQuery(
            from_at=from_at,
            to_at=exact_boundary + timedelta(microseconds=1),
            page=1,
            page_size=20,
        ),
    )

    assert daily.buckets == [{"timestamp": "2026-07-01T00:00:00Z", "count": 3}]


def test_event_query_applies_sql_narrowing_before_bounded_scope_scan(
    db_session,
    monkeypatch,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    created_at = datetime(2026, 7, 13, tzinfo=UTC)
    db_session.add_all(
        [
            Event(
                id=f"list-{index:04d}",
                project_id="repo-a",
                operation="memory.list",
                status=EventStatus.SUCCEEDED,
                request_json='{"app_id":"app-a"}',
                response_json="{}",
                error_json="{}",
                created_at=created_at,
                result_count=0,
                has_results=0,
            )
            for index in range(5001)
        ]
    )
    _add_trace_event(
        db_session,
        event_id="search-only",
        operation="memory.search",
        request={"app_id": "app-a"},
        created_at=created_at,
    )
    db_session.flush()
    repository = EventRepository(db_session)

    narrowed = repository.query_project_events(
        "repo-a",
        "app-a",
        repositories.EventQuery(
            operation="memory.search",
            page=1,
            page_size=20,
        ),
    )

    assert [item.id for item in narrowed.items] == ["search-only"]
    monkeypatch.setattr(db_session, "scalar", lambda *args, **kwargs: 5000)
    with pytest.raises(
        ValueError,
        match="^entity filter scan exceeds 5000 records$",
    ):
        repository.query_project_events(
            "repo-a",
            "app-a",
            repositories.EventQuery(
                operation="memory.list",
                page=1,
                page_size=20,
            ),
        )


def test_event_query_sql_narrows_canonical_app_before_scope_scan(db_session) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    created_at = datetime(2026, 7, 13, tzinfo=UTC)
    db_session.add_all(
        [
            Event(
                id=f"other-app-{index:04d}",
                project_id="repo-a",
                app_id="app-b",
                operation="memory.search",
                status=EventStatus.SUCCEEDED,
                request_json='{"app_id":"app-b"}',
                response_json="{}",
                error_json="{}",
                created_at=created_at,
                result_count=0,
                has_results=0,
            )
            for index in range(5001)
        ]
    )
    target = _add_trace_event(
        db_session,
        event_id="canonical-target",
        app_id="app-a",
        request={"app_id": "[REDACTED_URL]"},
        created_at=created_at,
    )
    db_session.flush()

    page = EventRepository(db_session).query_project_events(
        "repo-a",
        "app-a",
        repositories.EventQuery(page=1, page_size=20),
    )

    assert [item.id for item in page.items] == [target.id]


def _add_snapshot_retry_events(db_session) -> list[str]:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    base = datetime(2026, 7, 13, tzinfo=UTC)
    for event_id, created_at in (
        ("snapshot-old", base),
        ("snapshot-new", base + timedelta(seconds=1)),
    ):
        _add_trace_event(
            db_session,
            event_id=event_id,
            request={"app_id": "app-a"},
            created_at=created_at,
        )
    db_session.flush()
    return ["snapshot-new", "snapshot-old"]


def test_event_query_retries_the_whole_snapshot_once_when_page_fetch_changes(
    db_session,
    monkeypatch,
) -> None:
    expected_ids = _add_snapshot_retry_events(db_session)
    original_scalars = db_session.scalars
    fetch_count = 0

    def one_shot_missing(statement, *args, **kwargs):
        nonlocal fetch_count
        fetch_count += 1
        values = list(original_scalars(statement, *args, **kwargs))
        return iter(values[1:] if fetch_count == 1 else values)

    monkeypatch.setattr(db_session, "scalars", one_shot_missing)

    page = EventRepository(db_session).query_project_events(
        "repo-a",
        "app-a",
        repositories.EventQuery(page=1, page_size=20),
    )

    assert [item.id for item in page.items] == expected_ids
    assert page.total == 2
    assert sum(bucket["count"] for bucket in page.buckets) == 2
    assert fetch_count == 2


def test_event_query_fails_closed_when_page_snapshot_never_stabilizes(
    db_session,
    monkeypatch,
) -> None:
    _add_snapshot_retry_events(db_session)
    original_scalars = db_session.scalars
    fetch_count = 0

    def always_missing(statement, *args, **kwargs):
        nonlocal fetch_count
        fetch_count += 1
        values = list(original_scalars(statement, *args, **kwargs))
        return iter(values[1:])

    monkeypatch.setattr(db_session, "scalars", always_missing)

    with pytest.raises(
        ValueError,
        match=("^event query snapshot changed; retry with narrower filters$"),
    ):
        EventRepository(db_session).query_project_events(
            "repo-a",
            "app-a",
            repositories.EventQuery(page=1, page_size=20),
        )

    assert fetch_count == 2


def test_event_query_retries_when_loaded_page_fields_mutate(
    db_session,
    monkeypatch,
) -> None:
    expected_ids = _add_snapshot_retry_events(db_session)
    original_scalars = db_session.scalars
    fetch_count = 0

    def one_shot_mutation(statement, *args, **kwargs):
        nonlocal fetch_count
        fetch_count += 1
        values = list(original_scalars(statement, *args, **kwargs))
        if fetch_count != 1:
            return iter(values)
        changed = SimpleNamespace(
            **{
                column.name: getattr(values[0], column.name)
                for column in Event.__table__.columns
            }
        )
        changed.request_json = '{"app_id":"app-b"}'
        return iter([changed, *values[1:]])

    monkeypatch.setattr(db_session, "scalars", one_shot_mutation)

    page = EventRepository(db_session).query_project_events(
        "repo-a",
        "app-a",
        repositories.EventQuery(page=1, page_size=20),
    )

    assert [item.id for item in page.items] == expected_ids
    assert all(isinstance(item, Event) for item in page.items)
    assert fetch_count == 2
