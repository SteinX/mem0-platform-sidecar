import json

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from mem0_sidecar.core.extraction_policy import (
    ExtractionPolicyConflict,
    ExtractionSettings,
    apply_extraction_policy,
    get_extraction_policy,
    set_extraction_policy,
)
from mem0_sidecar.core.memory_ops import MemoryService
from mem0_sidecar.core.memory_scan_types import JsonObject
from mem0_sidecar.core.scope import normalize_scope
from mem0_sidecar.store.models import Category, MemoryIndex, Project
from mem0_sidecar.store.repositories import ProjectRepository


@pytest.fixture
def project(db_session):
    project = ProjectRepository(db_session).upsert_default_project(
        project_id="project-a", name="A", mem0_base_url="http://core.invalid"
    )
    project.settings_json = json.dumps({"service_capabilities": {"existing": True}})
    db_session.flush()
    return project


def test_app_override_is_versioned_scoped_and_preserves_other_settings(
    db_session, project
):
    set_extraction_policy(
        db_session, project_id=project.id, app_id=None, expected_version=0,
        settings=ExtractionSettings(enabled=True, user_instructions="Project rule"),
    )
    override = set_extraction_policy(
        db_session, project_id=project.id, app_id="app-a", expected_version=1,
        settings=ExtractionSettings(enabled=False),
    )
    assert override.version == 2
    assert not get_extraction_policy(db_session, project.id, "app-a").settings.enabled
    assert get_extraction_policy(db_session, project.id, "app-b").settings.enabled
    assert json.loads(project.settings_json)["service_capabilities"]["existing"]
    with pytest.raises(ExtractionPolicyConflict):
        set_extraction_policy(
            db_session, project_id=project.id, app_id="app-a", expected_version=1,
            settings=ExtractionSettings(enabled=True),
        )


def test_disabled_policy_is_an_exact_passthrough(db_session, project):
    scope = normalize_scope(
        project_id=project.id, app_id="app-a", user_id="user-a",
        agent_id=None, run_id=None,
    )
    payload = {"messages": [{"role": "user", "content": "Hello"}], "metadata": {}}
    assert apply_extraction_policy(db_session, scope, payload, []) is payload


def test_stale_session_cannot_overwrite_a_committed_policy(db_session, project):
    db_session.commit()
    with Session(db_session.get_bind()) as other:
        stale = other.get(Project, project.id)
        set_extraction_policy(
            db_session, project_id=project.id, app_id=None, expected_version=0,
            settings=ExtractionSettings(enabled=True),
        )
        db_session.commit()
        assert "extraction_policies" not in json.loads(stale.settings_json)
        with pytest.raises(ExtractionPolicyConflict):
            set_extraction_policy(
                other, project_id=project.id, app_id=None, expected_version=0,
                settings=ExtractionSettings(enabled=False),
            )


@pytest.mark.asyncio
async def test_policy_add_fetches_inferred_metadata_before_projection(
    db_session, project
):
    class Backend:
        def __init__(self) -> None:
            self.metadata: JsonObject = {}
            self.gets: list[str] = []

        async def add_memory(self, payload: JsonObject) -> JsonObject:
            self.metadata = dict(payload["metadata"])
            return {"results": [{"id": "fact-a", "memory": "Verified preference"}]}

        async def get_memory(self, memory_id: str) -> JsonObject:
            self.gets.append(memory_id)
            return {
                "id": memory_id, "memory": "Verified preference",
                "expiration_date": "2030-01-01",
                "metadata": {**self.metadata, "categories": ["alpha"],
                             "extraction_policy_version": 1},
            }

    db_session.add(Category(
        project_id=project.id, name="alpha", description="Verified preferences",
    ))
    set_extraction_policy(
        db_session, project_id=project.id, app_id="app-a", expected_version=0,
        settings=ExtractionSettings(enabled=True),
    )
    backend = Backend()
    project_id = project.id
    db_session.commit()
    await MemoryService(session=db_session, mem0=backend).add_memory(
        project_id=project_id,
        payload={"text": "Preference", "user_id": "user-a", "app_id": "app-a"},
    )
    stored = db_session.query(MemoryIndex).filter_by(mem0_memory_id="fact-a").one()
    assert backend.gets == ["fact-a"]
    assert stored.category == "alpha"
    assert stored.expires_at.date().isoformat() == "2030-01-01"
    assert json.loads(stored.metadata_projection_json)["extraction_policy_version"] == 1


@pytest.mark.parametrize(("user_id", "agent_id", "kind"), [
    ("user-a", "agent-a", "user_fact"),
    (None, "agent-a", "agent_experience"),
])
def test_policy_uses_descriptions_and_keeps_caller_values(
    db_session, project, user_id, agent_id, kind
):
    set_extraction_policy(
        db_session, project_id=project.id, app_id="app-a", expected_version=0,
        settings=ExtractionSettings(enabled=True, temporary_types={"beta": 2}),
    )
    scope = normalize_scope(
        project_id=project.id, app_id="app-a", user_id=user_id,
        agent_id=agent_id, run_id=None,
    )
    category = Category(
        name="beta", description="Temporary work", project_id=project.id
    )
    payload = {
        "prompt": "Caller rule", "expiration_date": None,
        "metadata": {"categories": ["beta"]},
    }
    result = apply_extraction_policy(db_session, scope, payload, [category])
    assert result["prompt"] == payload["prompt"]
    assert result["metadata"] == payload["metadata"]
    assert "expiration_date" in result and result["expiration_date"] is None
    assert result["extraction_policy"] == {
        "version": 1, "fact_kind": kind,
        "categories": [{
            "name": category.name, "description": category.description, "ttl_days": 2,
        }],
    }


@pytest.mark.parametrize(
    ("user_id", "agent_id"),
    [
        ("user-a", None),
        (None, "agent-a"),
    ],
)
def test_request_profiles_preserve_legacy_caller_prompt(
    db_session, project, user_id, agent_id
):
    set_extraction_policy(
        db_session,
        project_id=project.id,
        app_id="app-a",
        expected_version=0,
        settings=ExtractionSettings(
            enabled=True,
            allow_request_profiles=True,
            user_instructions="Operator user rule",
            agent_instructions="Operator agent rule",
        ),
    )
    scope = normalize_scope(
        project_id=project.id,
        app_id="app-a",
        user_id=user_id,
        agent_id=agent_id,
        run_id=None,
    )
    payload = {"prompt": "Caller legacy prompt", "messages": []}

    result = apply_extraction_policy(db_session, scope, payload, [])

    assert result["prompt"] == payload["prompt"]


@pytest.mark.parametrize("value", [True, "2", 0, -1, 3651])
def test_temporary_type_ttl_is_strict(value):
    with pytest.raises(ValidationError):
        ExtractionSettings(temporary_types={"beta": value})


@pytest.mark.parametrize(("track", "user", "agent", "kind", "instruction"), [
    ("personal", "alice", None, "user_fact", "Personal rule"),
    ("project", None, "repository", "agent_experience", "Project rule"),
])
def test_opt_in_track_uses_local_profile_without_mutating_categories(
    db_session, project, track, user, agent, kind, instruction,
):
    set_extraction_policy(
        db_session, project_id=project.id, app_id=None, expected_version=0,
        settings=ExtractionSettings(
            enabled=True, allow_request_profiles=True, dual_extraction_enabled=True,
        ),
    )
    scope = normalize_scope(
        project_id=project.id, app_id="app-a", user_id=user,
        agent_id=agent, run_id="session",
    )
    result = apply_extraction_policy(db_session, scope, {
        "infer": True, "extraction_track": track,
        "custom_instructions": "Personal rule",
        "agent_custom_instructions": "Project rule",
        "custom_categories": [{"decision": "Verified decision"}],
    }, [])
    assert result["prompt"] == instruction
    assert result["extraction_policy"] == {
        "version": 1, "fact_kind": kind,
        "categories": [{
            "name": "decision",
            "description": "Verified decision",
            "ttl_days": None,
        }],
    }
    assert not set(result).intersection({
        "extraction_track",
        "custom_instructions",
        "agent_custom_instructions",
        "custom_categories",
    })
    assert db_session.query(Category).count() == 0


def test_operator_profile_wins_and_disabled_category_cannot_be_reintroduced(
    db_session,
    project,
):
    set_extraction_policy(
        db_session,
        project_id=project.id,
        app_id=None,
        expected_version=0,
        settings=ExtractionSettings(
            enabled=True,
            allow_request_profiles=True,
            user_instructions="Operator rule",
        ),
    )
    db_session.add(Category(project_id=project.id, name="blocked", enabled=False))
    db_session.flush()
    scope = normalize_scope(
        project_id=project.id,
        app_id="app-a",
        user_id="alice",
        agent_id=None,
        run_id=None,
    )
    result = apply_extraction_policy(db_session, scope, {
        "prompt": "Caller prompt", "custom_instructions": "Caller rule",
        "custom_categories": ["blocked"],
    }, [])
    assert result["prompt"] == "Operator rule"
    policy = result.get("extraction_policy")
    assert isinstance(policy, dict)
    assert policy.get("categories") == []


def test_dual_is_disabled_by_default_and_compatibility_fields_do_not_reach_core(
    db_session,
    project,
):
    scope = normalize_scope(
        project_id=project.id,
        app_id="app-a",
        user_id="alice",
        agent_id=None,
        run_id=None,
    )
    payload = {
        "messages": [],
        "custom_instructions": "rule",
        "custom_categories": ["topic"],
    }
    assert apply_extraction_policy(db_session, scope, payload, []) == {"messages": []}
    with pytest.raises(ValueError, match="operator dual policy"):
        apply_extraction_policy(
            db_session,
            scope,
            {**payload, "extraction_track": "personal"},
            [],
        )
