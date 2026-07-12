import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from mem0_sidecar.core.explorer_filters import ExplorerFilter, ExplorerQuery
from mem0_sidecar.store.models import (
    Category,
    Entity,
    Event,
    EventStatus,
    ExportJob,
    ExportStatus,
    Job,
    JobStatus,
    MemoryIndex,
    Project,
)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class MemoryIndexPage:
    items: list[MemoryIndex]
    total: int
    scan_count: int


_MEMORY_FILTER_COLUMNS = {
    "user_id": MemoryIndex.user_id,
    "agent_id": MemoryIndex.agent_id,
    "app_id": MemoryIndex.app_id,
    "run_id": MemoryIndex.run_id,
    "memory_id": MemoryIndex.mem0_memory_id,
    "category": MemoryIndex.category,
}

_ENTITY_TYPE_COLUMNS = {
    "user": MemoryIndex.user_id,
    "agent": MemoryIndex.agent_id,
    "app": MemoryIndex.app_id,
    "run": MemoryIndex.run_id,
}


def _scalar_filter_expression(item: ExplorerFilter):
    if item.field == "entity_type":
        if item.operator == "in":
            values = item.value
            return or_(*(_ENTITY_TYPE_COLUMNS[value].is_not(None) for value in values))
        column = _ENTITY_TYPE_COLUMNS[item.value]
        if item.operator == "equals":
            return column.is_not(None)
        return column.is_(None)

    column = _MEMORY_FILTER_COLUMNS[item.field]
    if item.operator == "equals":
        return column == item.value
    if item.operator == "not_equals":
        return column != item.value
    return column.in_(item.value)


def _metadata_projection(memory: MemoryIndex) -> dict[str, Any]:
    try:
        value = json.loads(memory.metadata_projection_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _matches_filter(memory: MemoryIndex, item: ExplorerFilter) -> bool:
    if item.field == "metadata":
        projection = _metadata_projection(memory)
        expected = item.value
        return projection.get(expected["key"]) == expected["value"]

    if item.field == "entity_type":
        if item.operator == "in":
            return any(
                getattr(memory, _ENTITY_TYPE_COLUMNS[value].key) is not None
                for value in item.value
            )
        present = getattr(memory, _ENTITY_TYPE_COLUMNS[item.value].key) is not None
        return present if item.operator == "equals" else not present

    actual = getattr(memory, _MEMORY_FILTER_COLUMNS[item.field].key)
    if item.operator == "equals":
        return actual == item.value
    if item.operator == "not_equals":
        return actual is not None and actual != item.value
    return actual in item.value


def _matches_query_filters(memory: MemoryIndex, query: ExplorerQuery) -> bool:
    matches = (_matches_filter(memory, item) for item in query.filters)
    return all(matches) if query.match == "all" else any(matches)


def _memory_order_by(query: ExplorerQuery):
    if query.sort == "created_at_asc":
        return (MemoryIndex.created_at.asc(), MemoryIndex.mem0_memory_id.asc())
    return (MemoryIndex.created_at.desc(), MemoryIndex.mem0_memory_id.desc())


class ProjectRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_default_project(
        self,
        *,
        project_id: str,
        name: str,
        mem0_base_url: str,
        default_user_id: str | None = None,
        default_agent_id: str | None = None,
        default_app_id: str | None = None,
    ) -> Project:
        project = self.session.get(Project, project_id)
        if project is None:
            project = Project(
                id=project_id,
                name=name,
                default_user_id=default_user_id,
                default_app_id=default_app_id or project_id,
                default_agent_id=default_agent_id,
                mem0_base_url=mem0_base_url,
            )
            self.session.add(project)
        else:
            project.name = name
            project.mem0_base_url = mem0_base_url
            if default_user_id is not None:
                project.default_user_id = default_user_id
            if default_agent_id is not None:
                project.default_agent_id = default_agent_id
            if default_app_id is not None:
                project.default_app_id = default_app_id
        self.session.flush()
        return project


class CategoryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_project_category(self, project_id: str, category_id: str) -> Category:
        category = self.session.scalar(
            select(Category).where(
                Category.project_id == project_id, Category.id == category_id
            )
        )
        if category is None:
            raise KeyError(category_id)
        return category

    def find_project_category_by_name(
        self, project_id: str, name: str
    ) -> Category | None:
        return self.session.scalar(
            select(Category).where(
                Category.project_id == project_id, Category.name == name
            )
        )

    def create_project_category(
        self, project_id: str, item: dict[str, Any]
    ) -> Category:
        category = Category(
            project_id=project_id,
            name=str(item["name"]),
            description=str(item.get("description", "")),
            schema_json=_json(item.get("schema", {})),
            enabled=1 if bool(item.get("enabled", True)) else 0,
            strategy=str(item.get("strategy", "metadata")),
        )
        self.session.add(category)
        self.session.flush()
        return category

    def update_project_category(
        self, project_id: str, category_id: str, updates: dict[str, Any]
    ) -> Category:
        category = self.get_project_category(project_id, category_id)
        if "name" in updates:
            category.name = str(updates["name"])
        if "description" in updates:
            category.description = str(updates["description"])
        if "schema" in updates:
            category.schema_json = _json(updates["schema"])
        if "enabled" in updates:
            category.enabled = 1 if bool(updates["enabled"]) else 0
        if "strategy" in updates:
            category.strategy = str(updates["strategy"])
        category.version += 1
        self.session.flush()
        return category

    def delete_project_category(self, project_id: str, category_id: str) -> None:
        category = self.get_project_category(project_id, category_id)
        self.session.delete(category)
        self.session.flush()

    def replace_project_categories(
        self, *, project_id: str, categories: list[dict[str, Any]]
    ) -> list[Category]:
        self.session.execute(delete(Category).where(Category.project_id == project_id))
        self.session.flush()

        created: list[Category] = []
        for item in categories:
            category = Category(
                project_id=project_id,
                name=str(item["name"]),
                description=str(item.get("description", "")),
                schema_json=_json(item.get("schema", {})),
                enabled=1 if bool(item.get("enabled", True)) else 0,
                strategy=str(item.get("strategy", "metadata")),
            )
            self.session.add(category)
            created.append(category)

        self.session.flush()
        return created

    def list_project_categories(self, project_id: str) -> list[Category]:
        return list(
            self.session.scalars(
                select(Category).where(Category.project_id == project_id)
            )
        )


class EventRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_event(
        self,
        *,
        project_id: str,
        operation: str,
        request: dict[str, Any] | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
    ) -> Event:
        event = Event(
            project_id=project_id,
            operation=operation,
            status=EventStatus.PENDING,
            subject_type=subject_type,
            subject_id=subject_id,
            request_json=_json(request or {}),
        )
        self.session.add(event)
        self.session.flush()
        return event

    def get(self, event_id: str) -> Event:
        event = self.session.get(Event, event_id)
        if event is None:
            raise KeyError(event_id)
        return event

    def list_project_events(self, project_id: str) -> list[Event]:
        return list(
            self.session.scalars(
                select(Event)
                .where(Event.project_id == project_id)
                .order_by(Event.created_at, Event.id)
            )
        )

    def get_project_event(self, project_id: str, event_id: str) -> Event:
        event = self.session.scalar(
            select(Event).where(Event.project_id == project_id, Event.id == event_id)
        )
        if event is None:
            raise KeyError(event_id)
        return event

    def mark_succeeded(self, event_id: str, *, response: dict[str, Any]) -> Event:
        event = self.get(event_id)
        event.status = EventStatus.SUCCEEDED
        event.response_json = _json(response)
        event.completed_at = _utc_now()
        self.session.flush()
        return event

    def mark_failed(self, event_id: str, *, error: dict[str, Any]) -> Event:
        event = self.get(event_id)
        event.status = EventStatus.FAILED
        event.error_json = _json(error)
        event.completed_at = _utc_now()
        self.session.flush()
        return event


class MemoryIndexRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_memory(
        self,
        *,
        project_id: str,
        mem0_memory_id: str,
        app_id: str | None = None,
        include_deleted: bool = False,
    ) -> MemoryIndex | None:
        statement = select(MemoryIndex).where(
            MemoryIndex.project_id == project_id,
            MemoryIndex.mem0_memory_id == mem0_memory_id,
        )
        if app_id is not None:
            statement = statement.where(MemoryIndex.app_id == app_id)
        if not include_deleted:
            statement = statement.where(MemoryIndex.deleted_at.is_(None))
        return self.session.scalar(statement)

    def list_scoped_memory_ids(
        self,
        *,
        project_id: str,
        mem0_memory_ids: list[str],
        user_id: str | None,
        app_id: str | None,
        agent_id: str | None,
        run_id: str | None,
    ) -> set[str]:
        if not mem0_memory_ids:
            return set()

        statement = select(MemoryIndex.mem0_memory_id).where(
            MemoryIndex.project_id == project_id,
            MemoryIndex.deleted_at.is_(None),
            MemoryIndex.mem0_memory_id.in_(mem0_memory_ids),
        )
        if user_id is not None:
            statement = statement.where(MemoryIndex.user_id == user_id)
        if app_id is not None:
            statement = statement.where(MemoryIndex.app_id == app_id)
        if agent_id is not None:
            statement = statement.where(MemoryIndex.agent_id == agent_id)
        if run_id is not None:
            statement = statement.where(MemoryIndex.run_id == run_id)

        return set(self.session.scalars(statement))

    def list_export_candidates(
        self,
        *,
        project_id: str,
        filters: dict[str, Any],
    ) -> list[MemoryIndex]:
        statement = (
            select(MemoryIndex)
            .where(
                MemoryIndex.project_id == project_id,
                MemoryIndex.deleted_at.is_(None),
            )
            .order_by(MemoryIndex.created_at, MemoryIndex.mem0_memory_id)
        )
        if (user_id := filters.get("user_id")) is not None:
            statement = statement.where(MemoryIndex.user_id == user_id)
        if (app_id := filters.get("app_id")) is not None:
            statement = statement.where(MemoryIndex.app_id == app_id)
        if (agent_id := filters.get("agent_id")) is not None:
            statement = statement.where(MemoryIndex.agent_id == agent_id)
        if (run_id := filters.get("run_id")) is not None:
            statement = statement.where(MemoryIndex.run_id == run_id)
        return list(self.session.scalars(statement))

    def query_project_memories(
        self,
        project_id: str,
        app_id: str,
        query: ExplorerQuery,
    ) -> MemoryIndexPage:
        scope_conditions = [
            MemoryIndex.project_id == project_id,
            MemoryIndex.app_id == app_id,
            MemoryIndex.deleted_at.is_(None),
        ]
        if query.date_range.from_at is not None:
            scope_conditions.append(MemoryIndex.created_at >= query.date_range.from_at)
        if query.date_range.to_at is not None:
            scope_conditions.append(MemoryIndex.created_at <= query.date_range.to_at)

        scalar_filters = [item for item in query.filters if item.field != "metadata"]
        metadata_filters = [item for item in query.filters if item.field == "metadata"]
        scalar_expressions = [
            _scalar_filter_expression(item) for item in scalar_filters
        ]

        if not metadata_filters:
            conditions = list(scope_conditions)
            if scalar_expressions:
                combine = and_ if query.match == "all" else or_
                conditions.append(combine(*scalar_expressions))

            total = self.session.scalar(
                select(func.count()).select_from(MemoryIndex).where(*conditions)
            )
            offset = (query.page - 1) * query.page_size
            statement = (
                select(MemoryIndex)
                .where(*conditions)
                .order_by(*_memory_order_by(query))
                .offset(offset)
                .limit(query.page_size)
            )
            return MemoryIndexPage(
                items=list(self.session.scalars(statement)),
                total=total or 0,
                scan_count=0,
            )

        candidate_conditions = list(scope_conditions)
        if query.match == "all" and scalar_expressions:
            candidate_conditions.append(and_(*scalar_expressions))

        scan_count = self.session.scalar(
            select(func.count())
            .select_from(MemoryIndex)
            .where(*candidate_conditions)
        ) or 0
        if scan_count > 5000:
            raise ValueError("metadata filter scan exceeds 5000 records")

        candidates = list(
            self.session.scalars(
                select(MemoryIndex)
                .where(*candidate_conditions)
                .order_by(*_memory_order_by(query))
            )
        )
        matches = [
            memory
            for memory in candidates
            if _matches_query_filters(memory, query)
        ]
        offset = (query.page - 1) * query.page_size
        return MemoryIndexPage(
            items=matches[offset : offset + query.page_size],
            total=len(matches),
            scan_count=scan_count,
        )

    def mark_stale(
        self,
        project_id: str,
        mem0_memory_ids: Iterable[str],
    ) -> int:
        memory_ids = set(mem0_memory_ids)
        if not memory_ids:
            return 0

        memories = list(
            self.session.scalars(
                select(MemoryIndex).where(
                    MemoryIndex.project_id == project_id,
                    MemoryIndex.mem0_memory_id.in_(memory_ids),
                    MemoryIndex.deleted_at.is_(None),
                )
            )
        )
        stale_at = _utc_now()
        for memory in memories:
            memory.deleted_at = stale_at
        self.session.flush()
        return len(memories)

    def mark_stale_if_unchanged(
        self,
        *,
        project_id: str,
        app_id: str,
        mem0_memory_ids: Iterable[str],
        updated_at_lte: datetime,
    ) -> int:
        memory_ids = set(mem0_memory_ids)
        if not memory_ids:
            return 0

        result = self.session.execute(
            update(MemoryIndex)
            .where(
                MemoryIndex.project_id == project_id,
                MemoryIndex.app_id == app_id,
                MemoryIndex.mem0_memory_id.in_(memory_ids),
                MemoryIndex.deleted_at.is_(None),
                MemoryIndex.updated_at <= updated_at_lte,
            )
            .values(deleted_at=_utc_now())
            .execution_options(synchronize_session="fetch")
        )
        self.session.flush()
        return result.rowcount or 0

    def upsert_memory(
        self,
        *,
        project_id: str,
        mem0_memory_id: str,
        user_id: str | None,
        app_id: str | None,
        category: str | None,
        agent_id: str | None = None,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryIndex:
        memory = self.session.scalar(
            select(MemoryIndex).where(
                MemoryIndex.project_id == project_id,
                MemoryIndex.mem0_memory_id == mem0_memory_id,
            )
        )
        if memory is None:
            memory = MemoryIndex(project_id=project_id, mem0_memory_id=mem0_memory_id)
            self.session.add(memory)

        memory.user_id = user_id
        memory.agent_id = agent_id
        memory.app_id = app_id
        memory.run_id = run_id
        memory.category = category
        memory.metadata_projection_json = _json(metadata or {})
        memory.deleted_at = None
        self.session.flush()
        return memory

    def delete_memory(
        self,
        *,
        project_id: str,
        mem0_memory_id: str,
    ) -> MemoryIndex | None:
        memory = self.get_memory(project_id=project_id, mem0_memory_id=mem0_memory_id)
        if memory is None:
            return None

        memory.deleted_at = _utc_now()
        self.session.flush()
        return memory


class EntityRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_entity(
        self,
        *,
        project_id: str,
        entity_type: str,
        entity_id: str,
        display_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Entity:
        entity = self.session.scalar(
            select(Entity).where(
                Entity.project_id == project_id,
                Entity.entity_type == entity_type,
                Entity.entity_id == entity_id,
            )
        )
        if entity is None:
            entity = Entity(
                project_id=project_id,
                entity_type=entity_type,
                entity_id=entity_id,
            )
            self.session.add(entity)

        entity.display_name = display_name
        entity.metadata_json = _json(metadata or {})
        entity.last_seen_at = _utc_now()
        self.session.flush()
        return entity


class ExportJobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        project_id: str,
        export_format: str,
        filters: dict[str, Any],
    ) -> ExportJob:
        job = ExportJob(
            project_id=project_id,
            format=export_format,
            filters_json=_json(filters),
            status=ExportStatus.PENDING,
        )
        self.session.add(job)
        self.session.flush()
        return job

    def get(self, project_id: str, job_id: str) -> ExportJob:
        job = self.session.scalar(
            select(ExportJob).where(
                ExportJob.project_id == project_id,
                ExportJob.id == job_id,
            )
        )
        if job is None:
            raise KeyError(job_id)
        return job

    def list_project_exports(self, project_id: str) -> list[ExportJob]:
        return list(
            self.session.scalars(
                select(ExportJob)
                .where(ExportJob.project_id == project_id)
                .order_by(ExportJob.created_at.desc(), ExportJob.id.desc())
            )
        )

    def mark_running(self, project_id: str, job_id: str) -> ExportJob:
        job = self.get(project_id, job_id)
        job.status = ExportStatus.RUNNING
        job.started_at = _utc_now()
        self.session.flush()
        return job

    def mark_succeeded(
        self,
        project_id: str,
        job_id: str,
        *,
        result: dict[str, Any],
        total_count: int,
        exported_count: int,
        skipped_count: int,
    ) -> ExportJob:
        job = self.get(project_id, job_id)
        job.status = ExportStatus.SUCCEEDED
        job.result_json = _json(result)
        job.error_json = _json({})
        job.total_count = total_count
        job.exported_count = exported_count
        job.skipped_count = skipped_count
        job.completed_at = _utc_now()
        self.session.flush()
        return job

    def mark_failed(
        self, project_id: str, job_id: str, *, error: dict[str, Any]
    ) -> ExportJob:
        job = self.get(project_id, job_id)
        job.status = ExportStatus.FAILED
        job.error_json = _json(error)
        job.completed_at = _utc_now()
        self.session.flush()
        return job


class JobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def enqueue(
        self,
        *,
        project_id: str,
        event_id: str | None,
        job_type: str,
        payload: dict[str, Any],
    ) -> Job:
        job = Job(
            project_id=project_id,
            event_id=event_id,
            job_type=job_type,
            payload_json=_json(payload),
        )
        self.session.add(job)
        self.session.flush()
        return job

    def claim_next(self) -> Job | None:
        job = self.session.scalar(
            select(Job)
            .where(Job.status == JobStatus.PENDING)
            .order_by(Job.created_at)
        )
        if job is None:
            return None

        job.status = JobStatus.RUNNING
        job.locked_at = _utc_now()
        job.attempt_count += 1
        self.session.flush()
        return job
