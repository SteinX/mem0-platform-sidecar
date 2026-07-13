import json
from typing import Any

from mem0_sidecar.store.models import Event
from mem0_sidecar.store.repositories import EventRepository


def _event_to_dict(event: Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "project_id": event.project_id,
        "operation": event.operation,
        "status": event.status,
        "subject_type": event.subject_type,
        "subject_id": event.subject_id,
        "request": json.loads(event.request_json),
        "response": json.loads(event.response_json),
        "error": json.loads(event.error_json),
    }


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
            allow_project_scope=True,
        )
        return self.events.mark_succeeded(event.id, response=response)

    def list_project_events(self, project_id: str) -> list[dict[str, Any]]:
        return [
            _event_to_dict(event)
            for event in self.events.list_project_events(project_id)
        ]

    def get_project_event(self, project_id: str, event_id: str) -> dict[str, Any]:
        return _event_to_dict(self.events.get_project_event(project_id, event_id))
