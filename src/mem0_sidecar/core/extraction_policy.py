import json
from collections.abc import Sequence
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool
from sqlalchemy.orm import Session

from mem0_sidecar.core.scope import Scope
from mem0_sidecar.store.models import Category, Project
from mem0_sidecar.store.repositories import ProjectRepository


class ExtractionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: StrictBool = False
    user_instructions: str = Field(default="", max_length=8192)
    agent_instructions: str = Field(default="", max_length=8192)
    temporary_types: dict[
        str, Annotated[int, Field(strict=True, ge=1, le=3650)]
    ] = Field(default_factory=dict, max_length=64)


class ExtractionPolicyView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(default=0, ge=0)
    settings: ExtractionSettings = Field(default_factory=ExtractionSettings)


class ExtractionPolicyConflict(ValueError):
    pass


def get_extraction_policy(
    session: Session, project_id: str, app_id: str | None
) -> ExtractionPolicyView:
    project = session.get(Project, project_id)
    if project is None:
        raise KeyError(project_id)
    settings = json.loads(project.settings_json)
    policies = settings.get("extraction_policies", {})
    record = policies.get("apps", {}).get(app_id)
    if record is None:
        record = policies.get("project", {})
    return ExtractionPolicyView.model_validate(record)


def set_extraction_policy(
    session: Session,
    *,
    project_id: str,
    app_id: str | None,
    expected_version: int,
    settings: ExtractionSettings,
) -> ExtractionPolicyView:
    project = ProjectRepository(session).lock_for_mutation(project_id)
    session.refresh(project, ["settings_json"])
    current = get_extraction_policy(session, project_id, app_id)
    if current.version != expected_version:
        raise ExtractionPolicyConflict("Extraction policy version changed")
    updated = ExtractionPolicyView(version=current.version + 1, settings=settings)
    document = json.loads(project.settings_json)
    policies = document.setdefault("extraction_policies", {})
    if app_id is None:
        policies["project"] = updated.model_dump(mode="json")
    else:
        policies.setdefault("apps", {})[app_id] = updated.model_dump(mode="json")
    project.settings_json = json.dumps(document)
    session.flush()
    return updated


def apply_extraction_policy(
    session: Session,
    scope: Scope,
    payload: dict[str, JsonValue],
    categories: Sequence[Category],
) -> dict[str, JsonValue]:
    policy = get_extraction_policy(session, scope.project_id, scope.app_id)
    if not policy.settings.enabled or payload.get("infer") is False:
        return payload
    agent_scoped = bool(scope.agent_id) and not scope.user_id
    instructions = (
        policy.settings.agent_instructions
        if agent_scoped
        else policy.settings.user_instructions
    )
    result = dict(payload)
    if "prompt" not in payload and instructions:
        result["prompt"] = instructions
    result["extraction_policy"] = {
        "version": policy.version,
        "fact_kind": "agent_experience" if agent_scoped else "user_fact",
        "categories": [
            {
                "name": category.name,
                "description": category.description or category.name,
                "ttl_days": policy.settings.temporary_types.get(category.name),
            }
            for category in categories
        ],
    }
    return result
