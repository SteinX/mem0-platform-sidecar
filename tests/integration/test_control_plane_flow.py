from mem0_sidecar.core.categories import extract_category
from mem0_sidecar.core.events import EventService
from mem0_sidecar.core.projects import bootstrap_project
from mem0_sidecar.core.scope import normalize_scope
from mem0_sidecar.store.models import EventStatus
from mem0_sidecar.store.repositories import (
    CategoryRepository,
    EntityRepository,
    EventRepository,
    MemoryIndexRepository,
)


def test_control_plane_core_indexes_memory_with_category_event_and_entity(
    db_session,
) -> None:
    project = bootstrap_project(
        db_session,
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
        default_user_id="root",
        default_agent_id="codex",
    )
    CategoryRepository(db_session).replace_project_categories(
        project_id=project.id,
        categories=[{"name": "decision", "description": "Architecture decisions"}],
    )
    scope = normalize_scope(
        project_id=project.id,
        user_id="root",
        app_id=None,
        agent_id="codex",
        run_id="session-1",
    )
    category = extract_category({"type": "decision"}, {"decision"})
    memory = MemoryIndexRepository(db_session).upsert_memory(
        project_id=project.id,
        mem0_memory_id="mem-1",
        user_id=scope.user_id,
        app_id=scope.app_id,
        agent_id=scope.agent_id,
        run_id=scope.run_id,
        category=category,
        metadata={"type": "decision"},
    )
    entity = EntityRepository(db_session).upsert_entity(
        project_id=project.id,
        entity_type="app",
        entity_id=scope.app_id,
        display_name=scope.app_id,
    )
    event = EventService(EventRepository(db_session)).record_successful_mutation(
        project_id=project.id,
        operation="memory.add",
        subject_type="memory",
        subject_id=memory.mem0_memory_id,
        request={"metadata": {"type": "decision"}},
        response={"id": memory.mem0_memory_id},
    )
    db_session.commit()

    assert memory.category == "decision"
    assert entity.entity_id == "repo-a"
    assert event.status is EventStatus.SUCCEEDED
