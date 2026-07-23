from mem0_sidecar.store.models import (
    Category,
    ConsolidationLineage,
    ConsolidationPolicy,
    ConsolidationProposal,
    ConsolidationRun,
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


def test_consolidation_models_persist_without_memory_bodies(db_session) -> None:
    db_session.add(
        Project(
            id="repo-a",
            name="Repo A",
            default_app_id="app-a",
            mem0_base_url="http://mem0:8000",
        )
    )
    policy = ConsolidationPolicy(
        project_id="repo-a",
        app_id="app-a",
        policy_json='{"mode":"OBSERVE"}',
    )
    run = ConsolidationRun(
        project_id="repo-a",
        app_id="app-a",
        mode="OBSERVE",
    )
    db_session.add_all([policy, run])
    db_session.flush()
    proposal = ConsolidationProposal(
        run_id=run.id,
        project_id="repo-a",
        app_id="app-a",
        proposal_key="proposal-key",
        kind="EXACT_DUPLICATE",
        source_ids_json='["mem-1","mem-2"]',
        evidence_json='{"count":2}',
    )
    db_session.add(proposal)
    db_session.flush()
    db_session.add(
        ConsolidationLineage(
            project_id="repo-a",
            app_id="app-a",
            run_id=run.id,
            proposal_id=proposal.id,
            source_memory_id="mem-2",
            canonical_memory_id="mem-1",
            action="EXACT_DUPLICATE_DELETE",
            source_content_hash="abc123",
        )
    )
    db_session.commit()

    assert policy.enabled == 0
    assert run.status == "PENDING"
    assert proposal.status == "PENDING"
    assert "memory" not in proposal.evidence_json
    assert db_session.query(ConsolidationLineage).one().source_memory_id == (
        "mem-2"
    )


def test_memory_index_has_safe_consolidation_defaults(db_session) -> None:
    db_session.add(
        Project(
            id="repo-a",
            name="Repo A",
            mem0_base_url="http://mem0:8000",
        )
    )
    memory = MemoryIndex(project_id="repo-a", mem0_memory_id="mem-1")
    db_session.add(memory)
    db_session.commit()

    assert memory.pinned == 0
    assert memory.consolidation_state == "ACTIVE"
    assert memory.content_hash is None
    assert memory.shadowed_by_proposal_id is None
