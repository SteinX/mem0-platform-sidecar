from mem0_sidecar.store.models import (
    Category,
    Event,
    EventStatus,
    ExportJob,
    ExportStatus,
    Job,
    JobStatus,
    MemoryIndex,
    Project,
)


def test_control_plane_models_persist(db_session) -> None:
    project = Project(
        id="repo-a",
        name="Repo A",
        default_user_id="root",
        default_app_id="repo-a",
        default_agent_id="codex",
        mem0_base_url="http://mem0:8000",
    )
    db_session.add(project)
    db_session.add(
        Category(
            project_id="repo-a",
            name="decision",
            description="Architecture decisions",
        )
    )
    db_session.add(
        MemoryIndex(
            project_id="repo-a",
            mem0_memory_id="mem-1",
            user_id="root",
            app_id="repo-a",
            category="decision",
        )
    )
    event = Event(
        project_id="repo-a",
        operation="memory.add",
        status=EventStatus.SUCCEEDED,
        subject_type="memory",
        subject_id="mem-1",
    )
    db_session.add(event)
    db_session.flush()
    db_session.add(
        Job(
            project_id="repo-a",
            event_id=event.id,
            job_type="entity.rebuild",
            status=JobStatus.PENDING,
        )
    )
    db_session.add(
        ExportJob(
            project_id="repo-a",
            status=ExportStatus.PENDING,
            format="json",
            filters_json='{"app_id":"repo-a"}',
        )
    )
    db_session.commit()

    assert (
        db_session.get(Project, "repo-a").default_app_id == "repo-a"
    )
    assert (
        db_session.query(Category)
        .filter_by(project_id="repo-a")
        .one()
        .name
        == "decision"
    )
    assert (
        db_session.query(MemoryIndex)
        .filter_by(mem0_memory_id="mem-1")
        .one()
        .category
        == "decision"
    )
    assert (
        db_session.query(Event).filter_by(subject_id="mem-1").one().status
        is EventStatus.SUCCEEDED
    )
    assert db_session.query(Job).filter_by(project_id="repo-a").one().job_type == (
        "entity.rebuild"
    )
    assert (
        db_session.query(ExportJob).filter_by(project_id="repo-a").one().status
        is ExportStatus.PENDING
    )
