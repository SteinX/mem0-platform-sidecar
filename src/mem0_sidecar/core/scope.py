import unicodedata
from dataclasses import dataclass

_MAX_SCOPE_ID_CHARS = 256


def validate_scope_id(
    value: object,
    *,
    field_name: str,
    required: bool = True,
) -> str | None:
    """Validate an identifier before it reaches a portable SQL scope column."""

    if value is None:
        if not required:
            return None
        raise ValueError(
            f"{field_name} must be a portable 1-256 character identifier"
        )
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > _MAX_SCOPE_ID_CHARS
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ValueError(
            f"{field_name} must be a portable 1-256 character identifier"
        )
    return value


@dataclass(frozen=True)
class Scope:
    project_id: str
    user_id: str | None
    app_id: str
    agent_id: str | None
    run_id: str | None

    def as_filter_dict(self) -> dict[str, str]:
        values = {
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "app_id": self.app_id,
            "run_id": self.run_id,
        }
        return {key: value for key, value in values.items() if value}


def normalize_scope(
    *,
    project_id: str,
    user_id: str | None,
    app_id: str | None,
    agent_id: str | None,
    run_id: str | None,
) -> Scope:
    normalized_project_id = validate_scope_id(project_id, field_name="project_id")
    normalized_app_id = validate_scope_id(
        normalized_project_id if app_id is None else app_id,
        field_name="app_id",
    )
    return Scope(
        project_id=normalized_project_id,
        user_id=validate_scope_id(user_id, field_name="user_id", required=False),
        app_id=normalized_app_id,
        agent_id=validate_scope_id(agent_id, field_name="agent_id", required=False),
        run_id=validate_scope_id(run_id, field_name="run_id", required=False),
    )
