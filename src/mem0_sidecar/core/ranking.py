import json
import math
from datetime import UTC, date, datetime
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import case, update
from sqlalchemy.orm import Session

from mem0_sidecar.core.scope import Scope
from mem0_sidecar.store.models import MemoryIndex, Project
from mem0_sidecar.store.repositories import MemoryIndexRepository, ProjectRepository


class MemoryReadValidationError(ValueError):
    pass


def event_date(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError("event dates must use YYYY-MM-DD")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("event dates must use YYYY-MM-DD")
    return parsed


class SearchControls(BaseModel):
    model_config = ConfigDict(frozen=True)

    current_only: StrictBool = True
    event_from: date | None = None
    event_to: date | None = None

    @field_validator("event_from", "event_to", mode="before")
    @classmethod
    def parse_date(cls, value: object) -> date | None:
        return None if value is None else event_date(value)

    @model_validator(mode="after")
    def ordered_dates(self) -> "SearchControls":
        if self.event_from and self.event_to and self.event_from > self.event_to:
            raise ValueError("event_from must not be later than event_to")
        return self


def search_controls(payload: dict[str, Any]) -> SearchControls:
    try:
        return SearchControls.model_validate(payload)
    except ValidationError as exc:
        raise MemoryReadValidationError(str(exc)) from exc


class RankingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: StrictBool = False
    recency_weight: Annotated[
        float, Field(strict=True, ge=0, le=0.25, allow_inf_nan=False)
    ] = 0
    access_weight: Annotated[
        float, Field(strict=True, ge=0, le=0.25, allow_inf_nan=False)
    ] = 0
    half_life_days: Annotated[int, Field(strict=True, ge=1, le=3650)] = 90
    candidate_limit: Annotated[int, Field(strict=True, ge=1, le=200)] = 100


class RankingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(default=0, ge=0)
    settings: RankingSettings = Field(default_factory=RankingSettings)


class RankingPolicyConflict(ValueError):
    pass


def get_ranking_policy(session: Session, project_id: str) -> RankingPolicy:
    project = session.get(Project, project_id)
    if project is None:
        raise KeyError(project_id)
    return RankingPolicy.model_validate(
        json.loads(project.settings_json).get("ranking_policy", {})
    )


def set_ranking_policy(
    session: Session, project_id: str, expected_version: int, settings: RankingSettings
) -> RankingPolicy:
    project = ProjectRepository(session).lock_for_mutation(project_id)
    session.refresh(project, ["settings_json"])
    current = get_ranking_policy(session, project_id)
    if current.version != expected_version:
        raise RankingPolicyConflict("Ranking policy version changed")
    result = RankingPolicy(version=current.version + 1, settings=settings)
    document = json.loads(project.settings_json)
    document["ranking_policy"] = result.model_dump(mode="json")
    project.settings_json = json.dumps(document)
    session.flush()
    return result


def ranking_limit(payload: dict[str, Any]) -> int:
    limit = payload.get("top_k", 100)
    if type(limit) is not int or not 0 <= limit <= 200:
        raise MemoryReadValidationError("top_k must be an integer from 0 to 200")
    return limit


def _created_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (
        parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    )


def rank_results(
    response: Any,
    *,
    session: Session,
    scope: Scope,
    controls: SearchControls,
    policy: RankingPolicy,
    limit: int,
    now: datetime,
) -> Any:
    is_list = isinstance(response, list)
    rows = response if is_list else response.get("results", [])
    if not isinstance(rows, list):
        return response
    if len(rows) > 200:
        raise MemoryReadValidationError("Upstream exceeded the 200-candidate bound")
    projections = {
        row.mem0_memory_id: row
        for row in MemoryIndexRepository(session).list_memories_by_ids(
            project_id=scope.project_id,
            app_id=scope.app_id,
            mem0_memory_ids=[_record(item)["id"] for item in rows],
            include_shadowed=not controls.current_only,
        )
    }
    selected = []
    settings = policy.settings
    for item in rows:
        record = _record(item)
        projection = projections.get(record["id"])
        if projection is None:
            continue
        metadata = json.loads(projection.metadata_projection_json)
        if controls.event_from or controls.event_to:
            try:
                occurred = event_date(metadata.get("event_date"))
            except ValueError:
                continue
            if (controls.event_from and occurred < controls.event_from) or (
                controls.event_to and occurred > controls.event_to
            ):
                continue
        adjusted = dict(item)
        if settings.enabled:
            score = item.get("rerank_score", item.get("score"))
            if (
                isinstance(score, (int, float))
                and not isinstance(score, bool)
                and math.isfinite(score)
            ):
                created = _created_time(record.get("created_at"))
                age_days = (
                    max(0, (now - created).total_seconds() / 86400) if created else 0
                )
                evergreen = (
                    projection.pinned == 1
                    or metadata.get("evergreen") is True
                    or projection.normalized_type
                    in {
                        "decision",
                        "user_preference",
                        "project_profile",
                    }
                )
                decay = (
                    0
                    if evergreen
                    else settings.recency_weight
                    * (1 - 2 ** (-age_days / settings.half_life_days))
                )
                access = (
                    settings.access_weight
                    * projection.access_count
                    / (projection.access_count + 10)
                )
                adjusted["rank_score"] = score + abs(score) * (access - decay)
                adjusted["ranking_details"] = {
                    "policy_version": policy.version,
                    "recency_penalty": decay,
                    "access_bonus": access,
                    "evergreen": evergreen,
                    "source_created_at": record.get("created_at"),
                    "observed_at": projection.last_observed_at.isoformat()
                    if projection.last_observed_at
                    else None,
                    "event_date": metadata.get("event_date"),
                    "access_count": projection.access_count,
                    "last_accessed_at": projection.last_accessed_at.isoformat()
                    if projection.last_accessed_at
                    else None,
                }
        selected.append(adjusted)
    if settings.enabled and (settings.recency_weight or settings.access_weight):
        selected.sort(key=lambda row: row.get("rank_score", -math.inf), reverse=True)
    selected = selected[:limit]
    if settings.enabled and selected:
        session.execute(
            update(MemoryIndex)
            .where(
                MemoryIndex.project_id == scope.project_id,
                MemoryIndex.app_id == scope.app_id,
                MemoryIndex.deleted_at.is_(None),
                MemoryIndex.mem0_memory_id.in_(
                    {_record(row)["id"] for row in selected}
                ),
            )
            .values(
                access_count=case(
                    (
                        MemoryIndex.access_count < 1_000_000_000,
                        MemoryIndex.access_count + 1,
                    ),
                    else_=MemoryIndex.access_count,
                ),
                last_accessed_at=now,
                updated_at=MemoryIndex.updated_at,
            )
        )
    return (
        selected
        if is_list
        else {**response, "results": selected, "total": len(selected)}
    )


def _record(item: dict[str, Any]) -> dict[str, Any]:
    return item["memory"] if isinstance(item.get("memory"), dict) else item
