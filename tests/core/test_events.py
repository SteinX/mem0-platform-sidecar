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


def test_event_service_lists_and_gets_serialized_project_events(db_session) -> None:
    repo = EventRepository(db_session)
    service = EventService(repo)

    first = repo.create_event(
        project_id="repo-a",
        operation="memory.add",
        request={"text": "hello"},
        subject_type="memory",
        subject_id="mem-1",
    )
    repo.mark_succeeded(first.id, response={"id": "mem-1"})

    second = repo.create_event(
        project_id="repo-b",
        operation="memory.delete",
        request={"memory_id": "mem-2"},
        subject_type="memory",
        subject_id="mem-2",
    )
    repo.mark_failed(second.id, error={"message": "boom"})
    db_session.commit()

    listed = service.list_project_events("repo-a")
    fetched = service.get_project_event("repo-a", first.id)

    assert [event["id"] for event in listed] == [first.id]
    assert fetched["project_id"] == "repo-a"
    assert fetched["request"] == {"text": "hello"}
    assert fetched["response"] == {"id": "mem-1"}
    assert fetched["error"] == {}
