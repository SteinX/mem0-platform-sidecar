import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from mem0_sidecar.core.categories import extract_category
from mem0_sidecar.core.explorer_filters import ExplorerDateRange, ExplorerQuery
from mem0_sidecar.core.scope import Scope, normalize_scope
from mem0_sidecar.mem0_client.client import Mem0UpstreamError
from mem0_sidecar.observability import get_request_id
from mem0_sidecar.store.models import MemoryIndex, Project
from mem0_sidecar.store.repositories import (
    CategoryRepository,
    EntityRepository,
    EventRepository,
    MemoryIndexRepository,
)

SIDECAR_PROJECT_ID_METADATA_KEY = "_mem0_sidecar_project_id"
SIDECAR_APP_ID_METADATA_KEY = "_mem0_sidecar_app_id"
_MEMORY_PATCH_FIELDS = frozenset({"text", "metadata", "expiration_date"})
_RECONCILE_SCAN_LIMIT = 5000
_HYDRATION_BUFFER = 20
_HYDRATION_CONCURRENCY = 8


class MemoryUpstreamProtocolError(RuntimeError):
    """The upstream response does not satisfy the memory service contract."""


def _append_memory_id(memory_ids: list[str], candidate: Any) -> None:
    if isinstance(candidate, str) and candidate and candidate not in memory_ids:
        memory_ids.append(candidate)


def extract_memory_ids(response: dict[str, Any]) -> list[str]:
    memory_ids: list[str] = []
    _append_memory_id(memory_ids, response.get("id"))
    _append_memory_id(memory_ids, response.get("memory_id"))
    results = response.get("results")
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            _append_memory_id(memory_ids, item.get("id"))
            _append_memory_id(memory_ids, item.get("memory_id"))
    return memory_ids


def extract_memory_id(response: dict[str, Any]) -> str:
    memory_ids = extract_memory_ids(response)
    if memory_ids:
        return memory_ids[0]
    raise MemoryUpstreamProtocolError(
        f"Could not extract memory id from response: {response!r}"
    )


def _sidecar_scope_metadata(scope: Scope) -> dict[str, str]:
    return {
        SIDECAR_PROJECT_ID_METADATA_KEY: scope.project_id,
        SIDECAR_APP_ID_METADATA_KEY: scope.app_id,
    }


def _metadata_with_sidecar_scope(
    metadata: dict[str, Any] | None,
    *,
    scope: Scope,
) -> dict[str, Any]:
    scoped_metadata = dict(metadata or {})
    scoped_metadata.update(_sidecar_scope_metadata(scope))
    return scoped_metadata


def _filters_with_sidecar_scope(
    filters: dict[str, Any] | None,
    *,
    scope: Scope,
) -> dict[str, Any]:
    scoped_filters = dict(filters or {})
    scoped_filters.update(_sidecar_scope_metadata(scope))
    return scoped_filters


def _oss_add_payload(payload: dict[str, Any], *, scope: Scope) -> dict[str, Any]:
    oss_payload = dict(payload)
    oss_payload.pop("project_id", None)
    oss_payload.pop("app_id", None)
    oss_payload["metadata"] = _metadata_with_sidecar_scope(
        payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
        scope=scope,
    )
    return oss_payload


def _oss_search_payload(payload: dict[str, Any], *, scope: Scope) -> dict[str, Any]:
    oss_payload = dict(payload)
    oss_payload.pop("project_id", None)
    oss_payload.pop("app_id", None)
    oss_payload["filters"] = _filters_with_sidecar_scope(
        payload.get("filters") if isinstance(payload.get("filters"), dict) else None,
        scope=scope,
    )
    return oss_payload


def _result_memory_id(record: Any) -> str | None:
    if not isinstance(record, dict):
        return None
    memory_id = record.get("id") or record.get("memory_id")
    if isinstance(memory_id, str) and memory_id:
        return memory_id
    return None


def _memory_record_from_response(
    response: Any,
    *,
    expected_id: str,
) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise MemoryUpstreamProtocolError(
            "Upstream memory response must be an object"
        )
    if _result_memory_id(response) == expected_id:
        return response
    results = response.get("results")
    if isinstance(results, list):
        for item in results:
            if _result_memory_id(item) == expected_id:
                return item
    raise MemoryUpstreamProtocolError(
        f"Upstream memory response does not contain {expected_id!r}"
    )


def _record_metadata(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata")
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise MemoryUpstreamProtocolError(
            "Upstream memory record metadata must be an object or null"
        )
    return dict(metadata)


def _append_categories(categories: list[str], value: Any) -> None:
    values = value if isinstance(value, (list, tuple)) else [value]
    for candidate in values:
        if (
            isinstance(candidate, str)
            and candidate
            and candidate not in categories
        ):
            categories.append(candidate)


def _record_categories(record: dict[str, Any]) -> list[str]:
    metadata = _record_metadata(record)
    categories: list[str] = []
    _append_categories(categories, record.get("categories"))
    _append_categories(categories, metadata.get("categories"))
    for key in ("category", "custom_category", "type"):
        _append_categories(categories, metadata.get(key))
    return categories


def _normalize_memory_record(
    record: dict[str, Any],
    *,
    projection: MemoryIndex | None = None,
) -> dict[str, Any]:
    memory_id = _result_memory_id(record)
    if memory_id is None:
        raise MemoryUpstreamProtocolError(
            "Upstream memory record is missing an id"
        )

    metadata = _record_metadata(record)
    categories = _record_categories(record)

    normalized: dict[str, Any] = {
        "id": memory_id,
        "memory": record.get("memory", record.get("text")),
        "metadata": metadata,
        "categories": categories,
    }
    for field in ("user_id", "agent_id", "app_id", "run_id"):
        value = record.get(field)
        if value is None and projection is not None:
            value = getattr(projection, field)
        normalized[field] = value
    for field in ("created_at", "updated_at", "expiration_date"):
        value = record.get(field)
        if value is None and projection is not None and field != "expiration_date":
            projection_value = getattr(projection, field)
            value = projection_value.isoformat() if projection_value else None
        normalized[field] = value
    return normalized


def _validate_memory_patch(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        raise ValueError("Memory update payload must not be empty")
    unknown_fields = sorted(set(payload) - _MEMORY_PATCH_FIELDS)
    if unknown_fields:
        raise ValueError(
            f"Unsupported memory update fields: {', '.join(unknown_fields)}"
        )
    if "text" in payload and (
        not isinstance(payload["text"], str) or not payload["text"].strip()
    ):
        raise ValueError("text must be a non-empty string")
    if "metadata" in payload and payload["metadata"] is not None and not isinstance(
        payload["metadata"], dict
    ):
        raise ValueError("metadata must be an object or null")
    return {key: payload[key] for key in _MEMORY_PATCH_FIELDS if key in payload}


def _history_results(response: Any) -> list[Any]:
    if isinstance(response, list):
        return response
    if not isinstance(response, dict):
        raise MemoryUpstreamProtocolError(
            "Upstream history response must be an object or list"
        )
    for key in ("results", "history"):
        if key in response:
            value = response[key]
            if not isinstance(value, list):
                raise MemoryUpstreamProtocolError(
                    f"Upstream history response {key!r} must be a list"
                )
            return value
    raise MemoryUpstreamProtocolError(
        "Upstream history response has no recognized result container"
    )


def _list_results(response: Any) -> list[Any]:
    if not isinstance(response, dict):
        raise MemoryUpstreamProtocolError(
            "Upstream list response must be an object"
        )
    for key in ("results", "memories"):
        if key in response:
            value = response[key]
            if not isinstance(value, list):
                raise MemoryUpstreamProtocolError(
                    f"Upstream list response {key!r} must be a list"
                )
            return value
    raise MemoryUpstreamProtocolError(
        "Upstream list response has no recognized result container"
    )


def _filter_search_results(
    response: dict[str, Any],
    *,
    memory_repo: MemoryIndexRepository,
    scope: Scope,
) -> dict[str, Any]:
    results = response.get("results")
    if not isinstance(results, list):
        return response

    candidate_ids: list[str] = []
    for item in results:
        _append_memory_id(candidate_ids, _result_memory_id(item))

    allowed_ids = memory_repo.list_scoped_memory_ids(
        project_id=scope.project_id,
        mem0_memory_ids=candidate_ids,
        user_id=scope.user_id,
        app_id=scope.app_id,
        agent_id=scope.agent_id,
        run_id=scope.run_id,
    )
    filtered_results = [
        item
        for item in results
        if (memory_id := _result_memory_id(item)) is not None
        and memory_id in allowed_ids
    ]
    filtered_response = dict(response)
    filtered_response["results"] = filtered_results
    return filtered_response


def _event_payload(event: Any) -> dict[str, Any]:
    return {
        "id": event.id,
        "operation": event.operation,
        "status": event.status,
        "subject_type": event.subject_type,
        "subject_id": event.subject_id,
    }


def _error_payload(exc: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error_type": type(exc).__name__,
        "message": str(exc),
    }
    if request_id := get_request_id():
        payload["request_id"] = request_id
    if isinstance(exc, Mem0UpstreamError):
        payload["upstream_method"] = exc.method
        payload["upstream_path"] = exc.path
        payload["upstream_status_code"] = exc.status_code
        if exc.response_text is not None:
            payload["upstream_response_text"] = exc.response_text[:1000]
    return payload


def _validate_get_response(memory_id: str, response: Any) -> dict[str, Any]:
    if not isinstance(response, dict) or memory_id not in extract_memory_ids(response):
        raise KeyError(memory_id)
    return response


def _is_upstream_not_found(exc: Exception) -> bool:
    return isinstance(exc, Mem0UpstreamError) and exc.status_code == 404


def _effective_request_app_id(
    session: Session,
    *,
    project_id: str,
    request_app_id: str | None,
) -> str:
    if request_app_id:
        return request_app_id

    project = session.get(Project, project_id)
    if project is not None and project.default_app_id:
        return project.default_app_id

    return project_id


def _memory_delete_request(
    *,
    project_id: str,
    memory_id: str,
    memory: MemoryIndex | None,
    request_app_id: str | None = None,
) -> dict[str, Any]:
    if memory is None:
        scope = normalize_scope(
            project_id=project_id,
            user_id=None,
            app_id=request_app_id,
            agent_id=None,
            run_id=None,
        )
        request = {"memory_id": memory_id}
        request.update(scope.as_filter_dict())
        return request

    request = {
        "memory_id": memory.mem0_memory_id,
        "user_id": memory.user_id,
        "agent_id": memory.agent_id,
        "app_id": memory.app_id,
        "run_id": memory.run_id,
    }
    return {key: value for key, value in request.items() if value}


class MemoryService:
    def __init__(self, *, session: Session, mem0: Any) -> None:
        self.session = session
        self.mem0 = mem0

    async def add_memory(
        self,
        *,
        project_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        scope = normalize_scope(
            project_id=project_id,
            user_id=payload.get("user_id"),
            app_id=payload.get("app_id"),
            agent_id=payload.get("agent_id"),
            run_id=payload.get("run_id"),
        )
        metadata = dict(payload.get("metadata") or {})
        category_names = {
            category.name
            for category in CategoryRepository(self.session).list_project_categories(
                project_id
            )
            if category.enabled
        }
        category = extract_category(metadata, category_names)
        oss_payload = _oss_add_payload(payload, scope=scope)
        oss_payload.update(
            {
                key: value
                for key, value in {
                    "user_id": scope.user_id,
                    "agent_id": scope.agent_id,
                    "run_id": scope.run_id,
                }.items()
                if value
            }
        )

        event_repo = EventRepository(self.session)
        event = event_repo.create_event(
            project_id=project_id,
            app_id=scope.app_id,
            user_id=scope.user_id,
            agent_id=scope.agent_id,
            run_id=scope.run_id,
            operation="memory.add",
            request=oss_payload,
            subject_type="memory",
        )
        try:
            memory_response = await self.mem0.add_memory(oss_payload)
            memory_ids = extract_memory_ids(memory_response)
            if not memory_ids:
                raise MemoryUpstreamProtocolError(
                    f"Could not extract memory id from response: {memory_response!r}"
                )
            memory_repo = MemoryIndexRepository(self.session)
            for memory_id in memory_ids:
                memory_repo.upsert_memory(
                    project_id=project_id,
                    mem0_memory_id=memory_id,
                    user_id=scope.user_id,
                    app_id=scope.app_id,
                    agent_id=scope.agent_id,
                    run_id=scope.run_id,
                    category=category,
                    metadata=metadata,
                )
            EntityRepository(self.session).upsert_entity(
                project_id=project_id,
                entity_type="app",
                entity_id=scope.app_id,
                display_name=scope.app_id,
            )
            event.subject_id = memory_ids[0]
            event_repo.mark_succeeded(event.id, response=memory_response)
            return {"memory": memory_response, "event": _event_payload(event)}
        except Exception as exc:
            event_repo.mark_failed(event.id, error=_error_payload(exc))
            self.session.commit()
            raise

    async def search_memories(
        self,
        *,
        project_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        scope = normalize_scope(
            project_id=project_id,
            user_id=payload.get("user_id"),
            app_id=payload.get("app_id"),
            agent_id=payload.get("agent_id"),
            run_id=payload.get("run_id"),
        )
        oss_payload = _oss_search_payload(payload, scope=scope)
        oss_payload.update(
            {
                key: value
                for key, value in {
                    "user_id": scope.user_id,
                    "agent_id": scope.agent_id,
                    "run_id": scope.run_id,
                }.items()
                if value
            }
        )
        response = await self.mem0.search_memories(oss_payload)
        return _filter_search_results(
            response,
            memory_repo=MemoryIndexRepository(self.session),
            scope=scope,
        )

    async def query_memories(
        self,
        *,
        project_id: str,
        app_id: str,
        query: ExplorerQuery,
    ) -> dict[str, Any]:
        memory_repo = MemoryIndexRepository(self.session)
        offset = (query.page - 1) * query.page_size
        candidate_limit = query.page_size + _HYDRATION_BUFFER
        hydrated: dict[str, dict[str, Any]] = {}
        stale_skipped = 0

        def candidate_page() -> tuple[Any, list[MemoryIndex]]:
            window_query = replace(
                query,
                page=1,
                page_size=offset + candidate_limit,
            )
            page = memory_repo.query_project_memories(
                project_id,
                app_id,
                window_query,
            )
            return page, page.items[offset : offset + candidate_limit]

        async def hydrate(memory: MemoryIndex) -> tuple[str, dict[str, Any] | None]:
            try:
                response = await self.mem0.get_memory(memory.mem0_memory_id)
                record = _memory_record_from_response(
                    response,
                    expected_id=memory.mem0_memory_id,
                )
                return (
                    memory.mem0_memory_id,
                    _normalize_memory_record(record, projection=memory),
                )
            except Exception as exc:
                if _is_upstream_not_found(exc) or isinstance(
                    exc,
                    (
                        KeyError,
                        MemoryUpstreamProtocolError,
                        TypeError,
                        ValueError,
                    ),
                ):
                    return memory.mem0_memory_id, None
                raise

        _, candidates = candidate_page()
        semaphore = asyncio.Semaphore(_HYDRATION_CONCURRENCY)

        async def bounded_hydrate(
            memory: MemoryIndex,
        ) -> tuple[str, dict[str, Any] | None]:
            async with semaphore:
                return await hydrate(memory)

        hydration_results = await asyncio.gather(
            *(bounded_hydrate(memory) for memory in candidates)
        )
        stale_ids: list[str] = []
        for memory_id, normalized in hydration_results:
            if normalized is None:
                stale_ids.append(memory_id)
            else:
                hydrated[memory_id] = normalized

        stale_skipped = memory_repo.mark_stale(project_id, stale_ids)

        final_page, final_candidates = candidate_page()
        results = [
            hydrated[memory.mem0_memory_id]
            for memory in final_candidates
            if memory.mem0_memory_id in hydrated
        ][: query.page_size]
        return {
            "results": results,
            "total": final_page.total,
            "page": query.page,
            "page_size": query.page_size,
            "scan_count": final_page.scan_count,
            "stale_skipped": stale_skipped,
        }

    async def get_memory(
        self,
        *,
        project_id: str,
        memory_id: str,
        request_app_id: str | None = None,
    ) -> dict[str, Any]:
        effective_app_id = _effective_request_app_id(
            self.session,
            project_id=project_id,
            request_app_id=request_app_id,
        )
        memory_repo = MemoryIndexRepository(self.session)
        memory = memory_repo.get_memory(
            project_id=project_id,
            mem0_memory_id=memory_id,
            app_id=effective_app_id,
        )
        if memory is None:
            raise KeyError(memory_id)

        try:
            response = await self.mem0.get_memory(memory_id)
        except Exception as exc:
            if _is_upstream_not_found(exc):
                raise KeyError(memory_id) from exc
            raise
        return _validate_get_response(memory_id, response)

    async def update_memory(
        self,
        *,
        project_id: str,
        memory_id: str,
        request_app_id: str | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        patch = _validate_memory_patch(payload)
        effective_app_id = _effective_request_app_id(
            self.session,
            project_id=project_id,
            request_app_id=request_app_id,
        )
        memory_repo = MemoryIndexRepository(self.session)
        memory = memory_repo.get_memory(
            project_id=project_id,
            mem0_memory_id=memory_id,
            app_id=effective_app_id,
        )
        if memory is None:
            raise KeyError(memory_id)

        if "metadata" in patch:
            scope = normalize_scope(
                project_id=project_id,
                user_id=memory.user_id,
                app_id=memory.app_id,
                agent_id=memory.agent_id,
                run_id=memory.run_id,
            )
            patch["metadata"] = _metadata_with_sidecar_scope(
                patch["metadata"] if isinstance(patch["metadata"], dict) else None,
                scope=scope,
            )

        event_repo = EventRepository(self.session)
        event = event_repo.create_event(
            project_id=project_id,
            app_id=effective_app_id,
            user_id=memory.user_id,
            agent_id=memory.agent_id,
            run_id=memory.run_id,
            operation="memory.update",
            request=patch,
            subject_type="memory",
            subject_id=memory_id,
        )
        try:
            try:
                update_response = await self.mem0.update_memory(memory_id, patch)
            except ValueError as exc:
                raise MemoryUpstreamProtocolError(
                    "Upstream memory update response could not be decoded"
                ) from exc
            try:
                refresh_response = await self.mem0.get_memory(memory_id)
            except ValueError as exc:
                raise MemoryUpstreamProtocolError(
                    "Upstream memory refresh response could not be decoded"
                ) from exc
            record = _memory_record_from_response(
                refresh_response,
                expected_id=memory_id,
            )
            normalized = _normalize_memory_record(record, projection=memory)
            metadata = normalized["metadata"]
            categories = normalized["categories"]
            memory_repo.upsert_memory(
                project_id=project_id,
                mem0_memory_id=memory_id,
                user_id=normalized["user_id"],
                agent_id=normalized["agent_id"],
                app_id=effective_app_id,
                run_id=normalized["run_id"],
                category=categories[0] if categories else None,
                metadata=metadata,
            )
            event_repo.mark_succeeded(event.id, response=update_response)
            return {"memory": normalized, "event": _event_payload(event)}
        except Exception as exc:
            if _is_upstream_not_found(exc):
                memory_repo.mark_stale(project_id, [memory_id])
            event_repo.mark_failed(event.id, error=_error_payload(exc))
            self.session.commit()
            if _is_upstream_not_found(exc):
                raise KeyError(memory_id) from exc
            raise

    async def get_memory_history(
        self,
        *,
        project_id: str,
        memory_id: str,
        request_app_id: str | None,
    ) -> dict[str, Any]:
        effective_app_id = _effective_request_app_id(
            self.session,
            project_id=project_id,
            request_app_id=request_app_id,
        )
        memory = MemoryIndexRepository(self.session).get_memory(
            project_id=project_id,
            mem0_memory_id=memory_id,
            app_id=effective_app_id,
        )
        if memory is None:
            raise KeyError(memory_id)
        try:
            response = await self.mem0.get_memory_history(memory_id)
        except ValueError as exc:
            raise MemoryUpstreamProtocolError(
                "Upstream memory history response could not be decoded"
            ) from exc
        except Exception as exc:
            if _is_upstream_not_found(exc):
                raise KeyError(memory_id) from exc
            raise
        return {"results": _history_results(response)}

    async def reconcile_memories(
        self,
        *,
        project_id: str,
        app_id: str,
        adopt_unscoped: bool,
        allow_adopt_unscoped: bool,
        default_project_id: str,
    ) -> dict[str, int]:
        if adopt_unscoped and not allow_adopt_unscoped:
            raise ValueError("Unscoped memory adoption is disabled at runtime")
        if adopt_unscoped and project_id != default_project_id:
            raise ValueError(
                "Unscoped memories may only be adopted by the default project"
            )

        scan_cutoff = datetime.now(UTC)
        try:
            response = await self.mem0.list_memories(
                {"top_k": _RECONCILE_SCAN_LIMIT, "show_expired": True}
            )
        except ValueError as exc:
            raise MemoryUpstreamProtocolError(
                "Upstream memory list response could not be decoded"
            ) from exc
        records = _list_results(response)
        response_total = response.get("total")
        if "total" in response and (
            isinstance(response_total, bool)
            or not isinstance(response_total, int)
            or response_total < len(records)
        ):
            raise MemoryUpstreamProtocolError(
                "Upstream list response total is invalid"
            )
        memory_repo = MemoryIndexRepository(self.session)
        normalized_records: list[dict[str, Any]] = []
        for index, item in enumerate(records):
            if not isinstance(item, dict):
                raise MemoryUpstreamProtocolError(
                    f"Upstream memory record {index} must be an object"
                )
            try:
                normalized_records.append(_normalize_memory_record(item))
            except (MemoryUpstreamProtocolError, TypeError, ValueError) as exc:
                raise MemoryUpstreamProtocolError(
                    f"Upstream memory record {index} is malformed"
                ) from exc

        accepted_ids: set[str] = set()
        indexed = 0
        skipped_unscoped = 0
        skipped_other_scope = 0

        for normalized in normalized_records:
            metadata = normalized["metadata"]
            scoped_project_id = metadata.get(SIDECAR_PROJECT_ID_METADATA_KEY)
            scoped_app_id = metadata.get(SIDECAR_APP_ID_METADATA_KEY)
            has_project_scope = isinstance(scoped_project_id, str)
            has_app_scope = isinstance(scoped_app_id, str)
            is_unscoped = not has_project_scope and not has_app_scope
            is_matching_scope = (
                scoped_project_id == project_id and scoped_app_id == app_id
            )

            if is_unscoped:
                if not adopt_unscoped:
                    skipped_unscoped += 1
                    continue
            elif not is_matching_scope:
                skipped_other_scope += 1
                continue

            categories = normalized["categories"]
            memory_id = normalized["id"]
            if is_unscoped:
                claim = memory_repo.claim_memory(
                    project_id=project_id,
                    mem0_memory_id=memory_id,
                    user_id=normalized["user_id"],
                    agent_id=normalized["agent_id"],
                    app_id=app_id,
                    run_id=normalized["run_id"],
                    category=categories[0] if categories else None,
                    metadata=metadata,
                )
                if claim.status == "conflict":
                    skipped_other_scope += 1
                    continue
            else:
                memory_repo.upsert_memory(
                    project_id=project_id,
                    mem0_memory_id=memory_id,
                    user_id=normalized["user_id"],
                    agent_id=normalized["agent_id"],
                    app_id=app_id,
                    run_id=normalized["run_id"],
                    category=categories[0] if categories else None,
                    metadata=metadata,
                )
            accepted_ids.add(memory_id)
            indexed += 1

        truncated = len(records) >= _RECONCILE_SCAN_LIMIT or (
            isinstance(response_total, int) and response_total > len(records)
        )
        stale_marked = 0
        if not truncated:
            active_ids: set[str] = set()
            page_number = 1
            while True:
                active_page = memory_repo.query_project_memories(
                    project_id,
                    app_id,
                    ExplorerQuery(
                        match="all",
                        filters=(),
                        date_range=ExplorerDateRange(from_at=None, to_at=None),
                        page=page_number,
                        page_size=100,
                        sort="created_at_asc",
                    ),
                )
                active_ids.update(
                    memory.mem0_memory_id for memory in active_page.items
                )
                if page_number * 100 >= active_page.total:
                    break
                page_number += 1
            stale_marked = memory_repo.mark_stale_if_unchanged(
                project_id=project_id,
                app_id=app_id,
                mem0_memory_ids=active_ids - accepted_ids,
                updated_at_lte=scan_cutoff,
            )

        return {
            "scanned": len(records),
            "indexed": indexed,
            "skipped_unscoped": skipped_unscoped,
            "skipped_other_scope": skipped_other_scope,
            "stale_marked": stale_marked,
        }

    async def delete_memory(
        self,
        *,
        project_id: str,
        memory_id: str,
        request_app_id: str | None = None,
    ) -> dict[str, Any]:
        effective_app_id = _effective_request_app_id(
            self.session,
            project_id=project_id,
            request_app_id=request_app_id,
        )
        memory_repo = MemoryIndexRepository(self.session)
        memory = memory_repo.get_memory(
            project_id=project_id,
            mem0_memory_id=memory_id,
            app_id=effective_app_id,
        )
        if memory is None:
            event_repo = EventRepository(self.session)
            event = event_repo.create_event(
                project_id=project_id,
                app_id=effective_app_id,
                operation="memory.delete",
                request=_memory_delete_request(
                    project_id=project_id,
                    memory_id=memory_id,
                    memory=None,
                    request_app_id=effective_app_id,
                ),
                subject_type="memory",
                subject_id=memory_id,
            )
            event_repo.mark_failed(
                event.id,
                error={
                    "message": (
                        f"Memory {memory_id!r} not found for project {project_id!r}"
                    )
                },
            )
            self.session.commit()
            raise KeyError(memory_id)

        event_repo = EventRepository(self.session)
        event = event_repo.create_event(
            project_id=project_id,
            app_id=effective_app_id,
            user_id=memory.user_id,
            agent_id=memory.agent_id,
            run_id=memory.run_id,
            operation="memory.delete",
            request=_memory_delete_request(
                project_id=project_id,
                memory_id=memory_id,
                memory=memory,
            ),
            subject_type="memory",
            subject_id=memory_id,
        )
        try:
            response = await self.mem0.delete_memory(memory_id)
            memory_repo.delete_memory(
                project_id=project_id,
                mem0_memory_id=memory_id,
            )
            event_repo.mark_succeeded(event.id, response=response)
            return {"memory": response, "event": _event_payload(event)}
        except Exception as exc:
            event_repo.mark_failed(event.id, error=_error_payload(exc))
            self.session.commit()
            if _is_upstream_not_found(exc):
                raise KeyError(memory_id) from exc
            raise
