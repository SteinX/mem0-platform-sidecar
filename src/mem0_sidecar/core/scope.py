from dataclasses import dataclass


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
    normalized_app_id = app_id or project_id
    return Scope(
        project_id=project_id,
        user_id=user_id,
        app_id=normalized_app_id,
        agent_id=agent_id,
        run_id=run_id,
    )
