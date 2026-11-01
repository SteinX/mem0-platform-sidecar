import json
from collections.abc import Sequence
from typing import Annotated, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool
from sqlalchemy.orm import Session

from mem0_sidecar.core.scope import Scope
from mem0_sidecar.store.models import Category, Project
from mem0_sidecar.store.repositories import CategoryRepository, ProjectRepository


class ExtractionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: StrictBool = False
    allow_request_profiles: StrictBool = False
    dual_extraction_enabled: StrictBool = False
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
    plugin_fields = {
        "custom_instructions", "agent_custom_instructions", "custom_categories",
        "extraction_track",
    }
    has_profile = bool(plugin_fields.intersection(payload))
    request_categories = _request_categories(payload.get("custom_categories"))
    for key in ("custom_instructions", "agent_custom_instructions"):
        value = payload.get(key)
        if value is not None and (type(value) is not str or len(value) > 8192):
            raise ValueError(f"{key} must be a string of at most 8192 characters")
    track = payload.get("extraction_track")
    if track is not None:
        if track not in ("personal", "project"):
            raise ValueError("extraction_track must be personal or project")
        if track == "personal" and (not scope.user_id or scope.agent_id):
            raise ValueError("personal extraction requires only a user scope")
        if track == "project" and (not scope.agent_id or scope.user_id):
            raise ValueError("project extraction requires only an agent scope")
    policy = get_extraction_policy(session, scope.project_id, scope.app_id)
    if track is not None and (
        not policy.settings.enabled or not policy.settings.dual_extraction_enabled
        or payload.get("infer") is False
    ):
        raise ValueError(
            "Dual extraction requires an enabled operator dual policy and infer=true"
        )
    cleaned = {key: value for key, value in payload.items() if key not in plugin_fields}
    if not policy.settings.enabled or payload.get("infer") is False:
        return cleaned if has_profile else payload
    agent_scoped = bool(scope.agent_id) and not scope.user_id
    instructions = (
        policy.settings.agent_instructions
        if agent_scoped
        else policy.settings.user_instructions
    )
    result = cleaned
    if has_profile:
        if not instructions and policy.settings.allow_request_profiles:
            key = "agent_custom_instructions" if agent_scoped else "custom_instructions"
            instructions = payload.get(key)
        if instructions:
            result["prompt"] = instructions
        elif track is not None:
            result.pop("prompt", None)
    elif "prompt" not in payload and instructions:
        result["prompt"] = instructions
    effective_categories: list[dict[str, JsonValue]] = [
        {
            "name": category.name,
            "description": category.description or category.name,
            "ttl_days": policy.settings.temporary_types.get(category.name),
        }
        for category in categories
    ]
    if (
        policy.settings.allow_request_profiles
        and not CategoryRepository(session).list_project_categories(scope.project_id)
    ):
        effective_categories = request_categories
    result["extraction_policy"] = cast(
        JsonValue,
        {
            "version": policy.version,
            "fact_kind": "agent_experience" if agent_scoped else "user_fact",
            "categories": effective_categories,
        },
    )
    return result


def _request_categories(value: JsonValue) -> list[dict[str, JsonValue]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError("custom_categories must be a list of at most 64 categories")
    result = []
    names = set()
    for item in value:
        if isinstance(item, str):
            name, description = item, item
        elif isinstance(item, dict) and len(item) == 1:
            name, description = next(iter(item.items()))
        else:
            raise ValueError(
                "Each custom category must be a name or single name/description pair"
            )
        if (
            not isinstance(name, str) or not name.strip() or len(name) > 128
            or not isinstance(description, str) or not description.strip()
            or len(description) > 4096
            or name in names
        ):
            raise ValueError(
                "Custom categories require unique bounded names and descriptions"
            )
        names.add(name)
        result.append({"name": name, "description": description, "ttl_days": None})
    return result
