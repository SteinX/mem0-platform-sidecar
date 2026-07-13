import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import Index, UniqueConstraint, create_engine, insert
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
    event = event_repo.create_event(project_id=project.id, operation="memory.add")
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
        operation="memory.add",
        request={"text": "visible"},
        subject_type="memory",
        subject_id="mem-a",
    )
    event_repo.mark_succeeded(visible.id, response={"id": "mem-a"})

    hidden = event_repo.create_event(
        project_id="repo-b",
        operation="memory.add",
        request={"text": "hidden"},
        subject_type="memory",
        subject_id="mem-b",
    )
    event_repo.mark_succeeded(hidden.id, response={"id": "mem-b"})
    db_session.commit()

    listed = event_repo.list_project_events("repo-a")
    fetched = event_repo.get_project_event("repo-a", visible.id)

    assert [event.id for event in listed] == [visible.id]
    assert fetched.id == visible.id
    assert fetched.project_id == "repo-a"

    with pytest.raises(KeyError):
        event_repo.get_project_event("repo-a", hidden.id)


def test_event_model_matches_request_trace_migration() -> None:
    columns = Event.__table__.columns
    indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in Event.__table__.indexes
        if isinstance(index, Index)
    }

    assert columns["correlation_id"].nullable is True
    assert columns["latency_ms"].nullable is True
    assert columns["result_count"].nullable is False
    assert columns["has_results"].nullable is False
    assert columns["result_count"].default.arg == 0
    assert columns["has_results"].default.arg == 0
    assert str(columns["result_count"].server_default.arg) == "0"
    assert str(columns["has_results"].server_default.arg) == "0"
    assert indexes == {
        "ix_events_project_created": ("project_id", "created_at"),
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
        mem0_base_url="http://mem0:8000",
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
        },
    )

    assert event.started_at == started_at
    assert event.correlation_id == "request-123"
    assert json.loads(event.request_json) == {
        "api_key": "[REDACTED]",
        "app_id": "app-a",
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
        },
    )
    response = json.loads(succeeded.response_json)

    assert succeeded.status is EventStatus.SUCCEEDED
    assert succeeded.completed_at == completed_at
    assert succeeded.latency_ms == pytest.approx(125.0)
    assert succeeded.result_count == 2
    assert succeeded.has_results == 1
    assert response["authorization"] == "[REDACTED]"
    assert response["result_previews"] == [
        {"id": "mem-1", "memory": "first", "user_id": "alice"},
        {"id": "mem-2", "memory": "second", "score": 0.75},
    ]
    assert len(succeeded.request_json.encode()) <= 65_536
    assert len(succeeded.response_json.encode()) <= 65_536


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
        error={"token": "must-not-persist", "message": "x" * 80_000},
    )
    error = json.loads(failed.error_json)

    assert failed.status is EventStatus.FAILED
    assert failed.completed_at == completed_at
    assert failed.latency_ms == pytest.approx(250.0)
    assert failed.result_count == 0
    assert failed.has_results == 0
    assert error["token"] == "[REDACTED]"
    assert len(failed.error_json.encode()) <= 65_536


def _add_trace_event(
    db_session,
    *,
    event_id: str,
    project_id: str = "repo-a",
    operation: str = "memory.search",
    status: EventStatus = EventStatus.SUCCEEDED,
    request: object = None,
    created_at: datetime,
    result_count: int = 0,
) -> Event:
    event = Event(
        id=event_id,
        project_id=project_id,
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
    ]
    assert page.total == 2
    assert sum(bucket["count"] for bucket in page.buckets) == 2

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

    assert daily.buckets == [
        {"timestamp": "2026-07-01T00:00:00Z", "count": 3}
    ]


def test_event_query_applies_sql_narrowing_before_bounded_scope_scan(
    db_session,
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
