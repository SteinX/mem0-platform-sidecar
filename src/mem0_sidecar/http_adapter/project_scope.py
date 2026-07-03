from typing import Any

from fastapi import Request


def resolve_project_id(request: Request, payload: dict[str, Any] | None = None) -> str:
    if payload:
        for key in ("project_id", "app_id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value

    for key in ("project_id", "app_id"):
        value = request.query_params.get(key)
        if value:
            return value

    return request.app.state.settings.default_project_id
