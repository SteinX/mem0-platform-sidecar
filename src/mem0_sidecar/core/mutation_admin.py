from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mem0_sidecar.core.memory_ops import (
    SIDECAR_APP_ID_METADATA_KEY,
    SIDECAR_MUTATION_ID_METADATA_KEY,
    SIDECAR_PROJECT_ID_METADATA_KEY,
)
from mem0_sidecar.core.scope import validate_scope_id
from mem0_sidecar.store.models import MutationIntent, Project
from mem0_sidecar.store.repositories import (
    EventRepository,
    MutationIntentRepository,
    ProjectRepository,
)

MARKER_SCAN_LIMIT = 1000
MAX_RESOLUTION_REASON_CHARS = 512
RESOLVABLE_STATUSES = frozenset({"UNKNOWN", "EXHAUSTED"})


class MutationAdminError(RuntimeError):
    """A safe operator-facing mutation management rejection."""


def _safe_intent(intent: MutationIntent) -> dict[str, Any]:
    lease_expires_at = intent.lease_expires_at
    return {
        "id": intent.id,
        "operation": intent.operation,
        "status": intent.status,
        "attempt_count": intent.attempt_count,
        "lease_expires_at": (
            lease_expires_at.isoformat() if lease_expires_at is not None else None
        ),
    }


def _validate_reason(reason: object) -> str:
    if type(reason) is not str:
        raise MutationAdminError("resolution reason must be a string")
    normalized = reason.strip()
    if (
        not normalized
        or len(normalized) > MAX_RESOLUTION_REASON_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise MutationAdminError(
            "resolution reason must be 1-512 printable characters"
        )
    return normalized


def _lease_is_live(intent: MutationIntent) -> bool:
    lease_expires_at = intent.lease_expires_at
    if lease_expires_at is None:
        return False
    now = datetime.now(UTC)
    if lease_expires_at.tzinfo is None:
        now = now.replace(tzinfo=None)
    return lease_expires_at > now


class MutationAdminService:
    """Inspect and explicitly terminalize ambiguous durable mutation intents."""

    def __init__(self, *, session: Session, mem0: Any) -> None:
        self.session = session
        self.mem0 = mem0

    def list_blocking(self, *, project_id: str, app_id: str) -> dict[str, Any]:
        project_id = validate_scope_id(project_id, field_name="project_id")
        app_id = validate_scope_id(app_id, field_name="app_id")
        if self.session.get(Project, project_id) is None:
            raise MutationAdminError("project scope does not exist")
        intents = MutationIntentRepository(self.session).list_blocking(
            project_id,
            app_id,
        )
        safe_intents = [_safe_intent(intent) for intent in intents]
        return {"count": len(safe_intents), "intents": safe_intents}

    async def resolve_intent(
        self,
        *,
        project_id: str,
        app_id: str,
        intent_id: str,
        confirmation_intent_id: str,
        expected_status: str,
        expected_attempt_count: int,
        reason: str,
        accept_unknown_outcome: bool,
    ) -> dict[str, Any]:
        project_id = validate_scope_id(project_id, field_name="project_id")
        app_id = validate_scope_id(app_id, field_name="app_id")
        intent_id = validate_scope_id(intent_id, field_name="intent_id")
        confirmation_intent_id = validate_scope_id(
            confirmation_intent_id,
            field_name="intent_id",
        )
        reason = _validate_reason(reason)
        if intent_id != confirmation_intent_id:
            raise MutationAdminError("intent confirmation does not match")
        if not accept_unknown_outcome:
            raise MutationAdminError("unknown upstream outcome was not acknowledged")
        if type(expected_attempt_count) is not int or expected_attempt_count < 0:
            raise MutationAdminError("expected attempt count is invalid")

        ProjectRepository(self.session).lock_for_mutation(project_id)
        intent = self.session.scalar(
            select(MutationIntent).where(
                MutationIntent.id == intent_id,
                MutationIntent.project_id == project_id,
                MutationIntent.app_id == app_id,
            )
        )
        if intent is None:
            raise MutationAdminError("intent does not exist in the exact scope")
        if intent.status not in RESOLVABLE_STATUSES or _lease_is_live(intent):
            raise MutationAdminError("intent state cannot be resolved")
        if (
            intent.status != expected_status
            or intent.attempt_count != expected_attempt_count
        ):
            raise MutationAdminError("intent status or attempt count changed")

        intent_repo = MutationIntentRepository(self.session)
        marker: str | None = None
        if intent.operation == "memory.add":
            marker_value = intent_repo.payload(intent).get("mutation_id")
            if type(marker_value) is not str or not marker_value:
                raise MutationAdminError("add intent marker is unavailable")
            marker = marker_value
        operation = intent.operation
        self.session.rollback()

        if marker is not None:
            await self._refuse_observed_add_marker(
                marker=marker,
                project_id=project_id,
                app_id=app_id,
            )

        ProjectRepository(self.session).lock_for_mutation(project_id)
        intent = self.session.scalar(
            select(MutationIntent)
            .where(
                MutationIntent.id == intent_id,
                MutationIntent.project_id == project_id,
                MutationIntent.app_id == app_id,
            )
            .execution_options(populate_existing=True)
        )
        if intent is None:
            raise MutationAdminError("intent does not exist in the exact scope")
        if intent.status not in RESOLVABLE_STATUSES or _lease_is_live(intent):
            raise MutationAdminError("intent state cannot be resolved")
        if (
            intent.status != expected_status
            or intent.attempt_count != expected_attempt_count
            or intent.operation != operation
        ):
            raise MutationAdminError("intent status or attempt count changed")
        intent_repo = MutationIntentRepository(self.session)
        if marker is not None and (
            intent_repo.payload(intent).get("mutation_id") != marker
        ):
            raise MutationAdminError("intent status or attempt count changed")

        audit_repo = EventRepository(self.session)
        audit = audit_repo.create_event(
            project_id=project_id,
            app_id=app_id,
            operation="mutation.resolve",
            request={
                "intent_id": intent.id,
                "expected_status": expected_status,
                "expected_attempt_count": expected_attempt_count,
                "accept_unknown_outcome": True,
                "reason": reason,
            },
            subject_type="mutation_intent",
            subject_id=intent.id,
        )
        resolution_error = {
            "message": "Operator accepted an unknown upstream mutation outcome",
            "reason": reason,
        }
        audit_repo.mark_failed(intent.event_id, error=resolution_error)
        intent_repo.fail(intent.id, error=resolution_error)
        audit_repo.mark_succeeded(
            audit.id,
            response={"id": intent.id, "status": "FAILED"},
        )
        self.session.commit()
        return {
            "intent": _safe_intent(intent),
            "audit_event_id": audit.id,
        }

    async def _refuse_observed_add_marker(
        self,
        *,
        marker: str,
        project_id: str,
        app_id: str,
    ) -> None:
        response = await self.mem0.list_memories(
            {"top_k": MARKER_SCAN_LIMIT, "show_expired": True}
        )
        results = response.get("results") if isinstance(response, dict) else None
        if type(results) is not list:
            raise MutationAdminError("add marker observation is unavailable")
        if len(results) > MARKER_SCAN_LIMIT:
            raise MutationAdminError("add marker observation is incomplete")
        for item in results:
            if type(item) is not dict:
                continue
            metadata = item.get("metadata")
            if (
                type(metadata) is dict
                and metadata.get(SIDECAR_MUTATION_ID_METADATA_KEY) == marker
                and metadata.get(SIDECAR_PROJECT_ID_METADATA_KEY)
                == project_id
                and metadata.get(SIDECAR_APP_ID_METADATA_KEY) == app_id
            ):
                raise MutationAdminError(
                    "add marker was observed; intent cannot be resolved"
                )
