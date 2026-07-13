import json
from datetime import UTC, datetime

import pytest

from mem0_sidecar.core import events
from mem0_sidecar.core.events import EventService
from mem0_sidecar.store.models import Event, EventStatus
from mem0_sidecar.store.repositories import EventRepository, ProjectRepository


def _trace(event: Event) -> dict[str, object]:
    serializer = getattr(events, "event_to_trace_dict", None)
    assert callable(serializer), "event_to_trace_dict must be public"
    return serializer(event)


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
        app_id="app-a",
        operation="memory.add",
        request={"app_id": "app-a", "text": "hello"},
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
        allow_project_scope=True,
    )
    repo.mark_failed(second.id, error={"message": "boom"})
    db_session.commit()

    listed = service.list_project_events("repo-a")
    fetched = service.get_project_event("repo-a", "app-a", first.id)

    assert [event["id"] for event in listed] == [first.id]
    assert fetched["project_id"] == "repo-a"
    assert fetched["request"] == {"text": "hello"}
    assert fetched["response"] == {"id": "mem-1"}
    assert fetched["error"] == {}


def test_event_to_trace_dict_exposes_safe_public_trace_shape(db_session) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    repository = EventRepository(db_session)
    event = repository.create_event(
        project_id="repo-a",
        app_id="app-a",
        user_id="alice",
        agent_id="agent-a",
        run_id="run-a",
        operation="memory.search",
        correlation_id="request-123",
        request={
            "app_id": "app-a",
            "user_id": "alice",
            "agent_id": "agent-a",
            "run_id": "run-a",
            "query": "where are my notes?",
            "api_key": "must-not-leak",
            "metadata": {
                "_mem0_sidecar_project_id": "repo-a",
                "_mem0_sidecar_app_id": "app-a",
                "safe": "visible",
            },
        },
    )
    repository.mark_succeeded(
        event.id,
        response={
            "results": [
                {"id": f"mem-{index}", "memory": f"memory {index}"}
                for index in range(25)
            ],
            "total": 25,
        },
    )
    event.created_at = datetime(2026, 7, 13, 9, 59, tzinfo=UTC)
    event.started_at = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    event.completed_at = datetime(2026, 7, 13, 10, 0, 0, 125000, tzinfo=UTC)
    event.latency_ms = 125.0

    trace = _trace(event)

    assert trace == {
        "id": event.id,
        "correlation_id": "request-123",
        "operation": "memory.search",
        "display_operation": "SEARCH",
        "status": "SUCCEEDED",
        "entities": [
            {"type": "user", "id": "alice"},
            {"type": "agent", "id": "agent-a"},
            {"type": "app", "id": "app-a"},
            {"type": "run", "id": "run-a"},
        ],
        "request": {
            "api_key": "[REDACTED]",
            "metadata": {"safe": "visible"},
            "query": "where are my notes?",
        },
        "response": {"total": 25},
        "error": {},
        "result_count": 25,
        "has_results": True,
        "latency_ms": 125.0,
        "requested_at": "2026-07-13T10:00:00Z",
        "completed_at": "2026-07-13T10:00:00.125000Z",
        "result_previews": [
            {"id": f"mem-{index}", "memory": f"memory {index}"}
            for index in range(20)
        ],
        "result_previews_omitted": 5,
        "result_previews_scan_truncated": False,
    }
    serialized = json.dumps(trace, sort_keys=True)
    assert "repo-a" not in serialized
    assert "must-not-leak" not in serialized
    assert "_mem0_sidecar" not in serialized


def test_event_to_trace_dict_uses_bounded_legacy_fallbacks(db_session) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    event = Event(
        project_id="repo-a",
        app_id=None,
        user_id=None,
        agent_id=None,
        run_id=None,
        operation="memory.list",
        status=EventStatus.SUCCEEDED,
        request_json=json.dumps(
            {
                "project_id": "repo-a",
                "app_id": "legacy-app",
                "user_id": "legacy-user",
                "api_key": "legacy-secret",
                "internal_url": "http://mem0:8000/private",
                "page": 1,
            }
        ),
        response_json="not-json",
        error_json=json.dumps({"padding": "x" * 70_000}),
        result_count=-10,
        has_results=7,
        latency_ms=float("nan"),
        created_at=datetime(2026, 7, 13, 12, 0),
    )
    db_session.add(event)
    db_session.flush()

    trace = _trace(event)

    assert trace["display_operation"] == "GET ALL"
    assert trace["entities"] == [
        {"type": "user", "id": "legacy-user"},
        {"type": "app", "id": "legacy-app"},
    ]
    assert trace["request"] == {"api_key": "[REDACTED]", "page": 1}
    assert trace["response"] == {"_trace_invalid_json": True}
    assert trace["error"] == {
        "_trace_truncated": True,
        "reason": "legacy_json_too_large",
    }
    assert trace["result_count"] == 0
    assert trace["has_results"] is False
    assert trace["latency_ms"] is None
    assert trace["requested_at"] == "2026-07-13T12:00:00Z"
    assert len(json.dumps(trace).encode()) <= 65_536


def test_legacy_event_service_serialization_tolerates_non_object_json(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    event = Event(
        project_id="repo-a",
        app_id="app-a",
        operation="memory.add",
        status=EventStatus.FAILED,
        request_json="[]",
        response_json="null",
        error_json='"boom"',
    )
    db_session.add(event)
    db_session.commit()

    serialized = EventService(EventRepository(db_session)).get_project_event(
        "repo-a", "app-a", event.id
    )

    assert serialized["project_id"] == "repo-a"
    assert serialized["request"] == {"value": []}
    assert serialized["response"] == {"value": None}
    assert serialized["error"] == {"value": "boom"}


def test_event_to_trace_dict_rescrubs_legacy_string_secrets_and_internal_urls(
    db_session,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    event = Event(
        project_id="repo-a",
        app_id="app-a",
        operation="memory.search",
        status=EventStatus.SUCCEEDED,
        request_json=json.dumps(
            {
                "message": (
                    "authorization=Bearer legacy-secret at "
                    "http://mem0:8000/private"
                )
            }
        ),
        response_json=json.dumps(
            {
                "total": 3,
                "result_previews": [{"id": "mem-1", "memory": "one"}],
            }
        ),
        error_json="{}",
        result_count=-1,
    )
    db_session.add(event)
    db_session.flush()

    trace = _trace(event)
    serialized = json.dumps(trace, sort_keys=True)

    assert "legacy-secret" not in serialized
    assert "mem0:8000" not in serialized
    assert trace["request"] == {"message": "authorization=[REDACTED]"}
    assert trace["result_count"] == 3
    assert trace["has_results"] is True


@pytest.mark.parametrize("preview_count", [50, 75])
def test_event_to_trace_dict_caps_and_resanitizes_legacy_result_previews(
    db_session,
    preview_count: int,
) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    legacy_previews: list[object] = ["malformed-preview"] + [
        {
            "id": f"mem-{index}",
            "memory": f"memory {index}",
            "metadata": {"private": f"secret-preview-{index}"},
        }
        for index in range(preview_count - 1)
    ]
    event = Event(
        project_id="repo-a",
        app_id="app-a",
        operation="memory.search",
        status=EventStatus.SUCCEEDED,
        request_json="{}",
        response_json=json.dumps(
            {
                "result_previews": legacy_previews,
                "result_previews_omitted": 4,
            }
        ),
        error_json="{}",
        result_count=preview_count,
        has_results=1,
    )
    db_session.add(event)
    db_session.flush()

    trace = _trace(event)

    assert trace["result_previews"] == [
        {"id": f"mem-{index}", "memory": f"memory {index}"}
        for index in range(20)
    ]
    assert trace["result_previews_omitted"] == 4 + preview_count - 20
    assert "malformed-preview" not in json.dumps(trace)
    assert "secret-preview" not in json.dumps(trace)
