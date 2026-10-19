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


@pytest.mark.parametrize("value", [True, "2", 0, -1, 3651])
def test_temporary_type_ttl_is_strict(value):
    with pytest.raises(ValidationError):
        ExtractionSettings(temporary_types={"beta": value})
