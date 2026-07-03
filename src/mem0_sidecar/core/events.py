from typing import Any

from mem0_sidecar.store.models import Event
from mem0_sidecar.store.repositories import EventRepository


class EventService:
    def __init__(self, events: EventRepository) -> None:
        self.events = events

    def record_successful_mutation(
        self,
        *,
        project_id: str,
        operation: str,
        subject_type: str,
        subject_id: str,
        request: dict[str, Any],
        response: dict[str, Any],
    ) -> Event:
        event = self.events.create_event(
            project_id=project_id,
            operation=operation,
            request=request,
            subject_type=subject_type,
            subject_id=subject_id,
        )
        return self.events.mark_succeeded(event.id, response=response)
