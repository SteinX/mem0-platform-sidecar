import json

import pytest

from mem0_sidecar.store.models import EventStatus, ExportStatus, JobStatus, Project
from mem0_sidecar.store.repositories import (
    CategoryRepository,
    EntityRepository,
    EventRepository,
    ExportJobRepository,
    JobRepository,
    MemoryIndexRepository,
    ProjectRepository,
)


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
