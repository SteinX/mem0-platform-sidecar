from mem0_sidecar.core.events import EventService
from mem0_sidecar.store.models import EventStatus
from mem0_sidecar.store.repositories import EventRepository


def test_event_service_records_successful_mutation(db_session) -> None:
    service = EventService(EventRepository(db_session))

    event = service.record_successful_mutation(
        project_id="repo-a",
        operation="memory.add",
        subject_type="memory",
        subject_id="mem-1",
        request={"text": "Remember this"},
        response={"id": "mem-1"},
    )

    assert event.status is EventStatus.SUCCEEDED
    assert event.subject_id == "mem-1"
