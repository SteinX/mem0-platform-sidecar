from mem0_sidecar.store.models import EventStatus, JobStatus
from mem0_sidecar.store.repositories import (
    CategoryRepository,
    EntityRepository,
    EventRepository,
    JobRepository,
    MemoryIndexRepository,
    ProjectRepository,
)


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


def test_memory_index_repository_get_memory_ignores_deleted_by_default(db_session) -> None:
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
