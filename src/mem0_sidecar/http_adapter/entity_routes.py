import re
from typing import Annotated, Any
from urllib.parse import unquote

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session
from starlette.routing import Match
from starlette.types import Scope

from mem0_sidecar.core.entities import (
    EntityService,
    parse_entity_query,
    validate_entity_identity,
)
from mem0_sidecar.core.scope import validate_scope_id
from mem0_sidecar.http_adapter.dependencies import get_mem0_client, get_session
from mem0_sidecar.http_adapter.project_scope import (
    ensure_project,
    resolve_project_app_id,
)
from mem0_sidecar.store.models import Project

_ENCODED_OCTET = re.compile(r"%[0-9a-f]{2}", re.IGNORECASE)
_QUERY_KEYS = frozenset(
    {
        "project_id",
        "app_id",
        "entity_type",
        "match",
        "filters",
        "date_range",
        "page",
        "page_size",
    }
)
_FILTER_KEYS = frozenset({"field", "operator", "value"})
_DATE_RANGE_KEYS = frozenset({"from", "to"})
_REBUILD_KEYS = frozenset({"project_id", "app_id"})
_MAX_FILTERS = 64
_MAX_IN_VALUES = 100


class _RawEncodedEntityRoute(APIRoute):
    """Match encoded entity identifiers before ASGI path decoding loses shape."""

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        raw_path = scope.get("raw_path")
        if not isinstance(raw_path, bytes):
            return Match.NONE, {}

        index = 0
        while index < len(raw_path):
            if raw_path[index] != ord("%"):
                index += 1
                continue
            encoded_octet = raw_path[index + 1 : index + 3]
            if len(encoded_octet) != 2 or any(
                byte not in b"0123456789abcdefABCDEF" for byte in encoded_octet
            ):
                return Match.NONE, {}
            index += 3

        try:
            encoded_path = raw_path.decode("ascii", "strict")
        except UnicodeDecodeError:
            return Match.NONE, {}
        return super().matches({**scope, "path": encoded_path})


entity_router = APIRouter(route_class=_RawEncodedEntityRoute)
SessionDependency = Annotated[Session, Depends(get_session)]
Mem0Dependency = Annotated[Any, Depends(get_mem0_client)]
JsonBody = Annotated[Any, Body()]


def _decode_path_identifier(
    value: str,
    *,
    field_name: str,
    allow_encoded_octet_literal: bool = False,
) -> str:
    try:
        decoded = unquote(value, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name.replace('_', ' ')}",
        ) from exc

    has_traversal_segment = any(
        segment in {".", ".."} for segment in re.split(r"[\\/]", decoded)
    )
    if (
        has_traversal_segment
        or (
            not allow_encoded_octet_literal
            and _ENCODED_OCTET.search(decoded)
        )
        or any(ord(character) < 32 or ord(character) == 127 for character in decoded)
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name.replace('_', ' ')}",
        )
    return decoded


def _unknown_fields_message(prefix: str, fields: set[object]) -> str:
    names = ", ".join(sorted(str(field) for field in fields))
    return f"unknown {prefix} fields: {names}"


def _require_object(payload: Any, *, name: str) -> dict[str, Any]:
    if type(payload) is not dict:
        raise ValueError(f"{name} must be an object")
    return payload


def _parse_route_query(payload: Any):
    query_payload = _require_object(payload, name="query")
    unknown_keys = set(query_payload) - _QUERY_KEYS
    if unknown_keys:
        raise ValueError(_unknown_fields_message("query", unknown_keys))

    raw_date_range = query_payload.get("date_range")
    if isinstance(raw_date_range, dict):
        unknown_date_keys = set(raw_date_range) - _DATE_RANGE_KEYS
        if unknown_date_keys:
            raise ValueError(
                _unknown_fields_message("date_range", unknown_date_keys)
            )

    raw_filters = query_payload.get("filters")
    if isinstance(raw_filters, list):
        if len(raw_filters) > _MAX_FILTERS:
            raise ValueError(f"filters must contain at most {_MAX_FILTERS} items")
        for index, raw_filter in enumerate(raw_filters):
            if not isinstance(raw_filter, dict):
                continue
            unknown_filter_keys = set(raw_filter) - _FILTER_KEYS
            if unknown_filter_keys:
                raise ValueError(
                    _unknown_fields_message(
                        f"filters[{index}]",
                        unknown_filter_keys,
                    )
                )
            if raw_filter.get("operator") == "in":
                raw_value = raw_filter.get("value")
                if isinstance(raw_value, list) and len(raw_value) > _MAX_IN_VALUES:
                    raise ValueError(
                        f"filters[{index}].value must contain at most "
                        f"{_MAX_IN_VALUES} items"
                    )
    return parse_entity_query(query_payload)


def _parse_rebuild_payload(payload: Any) -> dict[str, Any]:
    rebuild_payload = _require_object(payload, name="rebuild request")
    unknown_keys = set(rebuild_payload) - _REBUILD_KEYS
    if unknown_keys:
        raise ValueError(_unknown_fields_message("rebuild request", unknown_keys))
    return rebuild_payload


def _explicit_scope_value(
    request: Request,
    payload: dict[str, Any] | None,
    field_name: str,
) -> str | None:
    if payload is not None and field_name in payload:
        return validate_scope_id(payload[field_name], field_name=field_name)
    values = request.query_params.getlist(field_name)
    if len(values) > 1:
        raise ValueError(f"{field_name} must be provided at most once")
    if values:
        return validate_scope_id(values[0], field_name=field_name)
    return None


def _resolve_entity_scope(
    request: Request,
    session: Session,
    payload: dict[str, Any] | None = None,
    *,
    missing_detail: str,
) -> tuple[str, str]:
    explicit_project_id = _explicit_scope_value(request, payload, "project_id")
    project_id = explicit_project_id or validate_scope_id(
        request.app.state.settings.default_project_id,
        field_name="project_id",
    )
    requested_app_id = _explicit_scope_value(request, payload, "app_id")
    app_id = resolve_project_app_id(
        session,
        project_id=project_id,
        request_app_id=requested_app_id,
    )
    if app_id is None:
        raise HTTPException(status_code=404, detail=missing_detail)
    return project_id, validate_scope_id(app_id, field_name="app_id")


@entity_router.post("/v1/entities/query")
def query_entities(
    payload: JsonBody,
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    try:
        query = _parse_route_query(payload)
        project_id, app_id = _resolve_entity_scope(
            request,
            session,
            payload,
            missing_detail="Project not found",
        )
        result = EntityService(session=session, mem0=mem0).query_entities(
            project_id,
            app_id,
            query,
        )
    except HTTPException:
        session.rollback()
        raise
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        session.rollback()
        raise

    return {
        "results": result["results"],
        "page": result["page"],
        "page_size": result["page_size"],
        "total": result["total"],
        "has_more": result["page"] * result["page_size"] < result["total"],
    }


@entity_router.get("/v1/entities/{entity_type}/{entity_id}")
def get_entity(
    entity_type: str,
    entity_id: str,
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    try:
        decoded_type = _decode_path_identifier(
            entity_type,
            field_name="entity_type",
        )
        decoded_id = _decode_path_identifier(
            entity_id,
            field_name="entity_id",
            allow_encoded_octet_literal=True,
        )
        decoded_type, decoded_id = validate_entity_identity(
            decoded_type,
            decoded_id,
        )
        project_id, app_id = _resolve_entity_scope(
            request,
            session,
            missing_detail="Entity not found",
        )
        return EntityService(session=session, mem0=mem0).get_entity(
            project_id,
            app_id,
            decoded_type,
            decoded_id,
        )
    except HTTPException:
        session.rollback()
        raise
    except KeyError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="Entity not found") from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        session.rollback()
        raise


@entity_router.delete("/v1/entities/{entity_type}/{entity_id}")
async def delete_entity(
    entity_type: str,
    entity_id: str,
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    try:
        decoded_type = _decode_path_identifier(
            entity_type,
            field_name="entity_type",
        )
        decoded_id = _decode_path_identifier(
            entity_id,
            field_name="entity_id",
            allow_encoded_octet_literal=True,
        )
        decoded_type, decoded_id = validate_entity_identity(
            decoded_type,
            decoded_id,
        )
        project_id, app_id = _resolve_entity_scope(
            request,
            session,
            missing_detail="Entity not found",
        )
        result = await EntityService(session=session, mem0=mem0).delete_entity(
            project_id,
            app_id,
            decoded_type,
            decoded_id,
        )
        session.commit()
        return result
    except HTTPException:
        session.rollback()
        raise
    except KeyError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="Entity not found") from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        session.rollback()
        raise


@entity_router.post("/v1/projects/{path_project_id}/entities/rebuild")
def rebuild_entities(
    path_project_id: str,
    payload: JsonBody,
    request: Request,
    session: SessionDependency,
    mem0: Mem0Dependency,
) -> dict[str, Any]:
    try:
        decoded_path_project_id = validate_scope_id(
            _decode_path_identifier(
                path_project_id,
                field_name="project_id",
            ),
            field_name="project_id",
        )
        rebuild_payload = _parse_rebuild_payload(payload)
        explicit_project_id = _explicit_scope_value(
            request,
            rebuild_payload,
            "project_id",
        )
        project_id = explicit_project_id or validate_scope_id(
            request.app.state.settings.default_project_id,
            field_name="project_id",
        )
        if project_id != decoded_path_project_id:
            raise HTTPException(status_code=403, detail="Project scope mismatch")

        default_project_id = validate_scope_id(
            request.app.state.settings.default_project_id,
            field_name="project_id",
        )
        if session.get(Project, project_id) is None:
            if project_id != default_project_id:
                raise HTTPException(status_code=404, detail="Project not found")
            ensure_project(session, request.app.state.settings, project_id)

        requested_app_id = _explicit_scope_value(
            request,
            rebuild_payload,
            "app_id",
        )
        app_id = resolve_project_app_id(
            session,
            project_id=project_id,
            request_app_id=requested_app_id,
        )
        if app_id is None:
            raise HTTPException(status_code=404, detail="Project not found")
        app_id = validate_scope_id(app_id, field_name="app_id")

        result = EntityService(session=session, mem0=mem0).rebuild_entities(
            project_id,
            app_id,
        )
        session.commit()
        return {
            "entities": result["entities"],
            "project_id": project_id,
            "app_id": app_id,
        }
    except HTTPException:
        session.rollback()
        raise
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        session.rollback()
        raise
