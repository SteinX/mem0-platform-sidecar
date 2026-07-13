import os
import sys
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.store.models import Event, MemoryIndex

pytestmark = pytest.mark.e2e


def _live_settings(
    tmp_path,
    *,
    project_id: str | None = None,
    database_name: str = "sidecar-e2e.sqlite3",
) -> SidecarSettings:
    base_url = os.environ.get("MEM0_E2E_BASE_URL")
    if not base_url:
        pytest.skip("MEM0_E2E_BASE_URL is not set")
    project_id = project_id or os.environ.get(
        "MEM0_E2E_PROJECT_ID", "sidecar-e2e"
    )
    return SidecarSettings(
        database_url=f"sqlite:///{tmp_path / database_name}",
        mem0_base_url=base_url,
        mem0_api_key=os.environ.get("MEM0_E2E_API_KEY"),
        default_project_id=project_id,
    )


def _record_contains(record, needle: str) -> bool:
    if isinstance(record, dict):
        return any(_record_contains(value, needle) for value in record.values())
    if isinstance(record, (list, tuple)):
        return any(_record_contains(value, needle) for value in record)
    if isinstance(record, str):
        return needle in record
    return record == needle


def _extract_memory_ids(payload: dict[str, object]) -> list[str]:
    ids: list[str] = []

    def add(candidate: object) -> None:
        if isinstance(candidate, str) and candidate not in ids:
            ids.append(candidate)

    add(payload.get("id"))
    add(payload.get("memory_id"))
    results = payload.get("results")
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            add(item.get("id"))
            add(item.get("memory_id"))
    return ids


def _scoped_params(*, project_id: str, app_id: str) -> dict[str, str]:
    return {"project_id": project_id, "app_id": app_id}


def _delete_scoped_memory(
    client: TestClient,
    *,
    memory_id: str,
    project_id: str,
    app_id: str,
) -> str | None:
    try:
        response = client.delete(
            f"/v1/memories/{memory_id}",
            params=_scoped_params(project_id=project_id, app_id=app_id),
        )
    except Exception as exc:
        return f"Scoped cleanup raised {type(exc).__name__}: {exc}"
    if response.status_code != 200:
        if response.status_code == 404:
            return "Scoped cleanup returned HTTP 404 with active projection"
        return (
            f"Scoped cleanup returned HTTP {response.status_code}: "
            f"{response.text}"
        )
    return None


def _add_direct_upstream_memory(
    settings: SidecarSettings,
    *,
    text: str,
    user_id: str,
    run_id: str,
    metadata: dict[str, object],
) -> str:
    response = httpx.post(
        f"{settings.mem0_base_url.rstrip('/')}/memories",
        json={
            "messages": [{"role": "user", "content": text}],
            "user_id": user_id,
            "run_id": run_id,
            "infer": False,
            "metadata": metadata,
        },
        timeout=30,
    )
    response.raise_for_status()
    memory_ids = _extract_memory_ids(response.json())
    assert memory_ids, response.text
    return memory_ids[0]


def _delete_direct_upstream_memory(
    settings: SidecarSettings,
    memory_id: str,
) -> str | None:
    try:
        response = httpx.delete(
            f"{settings.mem0_base_url.rstrip('/')}/memories/{memory_id}",
            timeout=30,
        )
    except Exception as exc:
        return f"Direct upstream cleanup raised {type(exc).__name__}: {exc}"
    if response.status_code not in {200, 404}:
        return (
            f"Direct upstream cleanup returned HTTP {response.status_code}: "
            f"{response.text}"
        )
    return None


def _upstream_absence_error(
    settings: SidecarSettings,
    memory_id: str,
) -> str | None:
    try:
        response = httpx.get(
            f"{settings.mem0_base_url.rstrip('/')}/memories",
            params={"top_k": 5000, "show_expired": "true"},
            timeout=30,
        )
    except Exception as exc:
        return f"Upstream cleanup verification raised {type(exc).__name__}: {exc}"
    if response.status_code != 200:
        return (
            f"Upstream cleanup verification returned HTTP "
            f"{response.status_code}: {response.text}"
        )
    try:
        memory_ids = _extract_memory_ids(response.json())
    except Exception as exc:
        return (
            "Upstream cleanup verification could not decode the list response: "
            f"{type(exc).__name__}: {exc}"
        )
    if memory_id in memory_ids:
        return f"Upstream cleanup verification still found {memory_id}"
    return None


def _projection_absence_error(
    client: TestClient,
    *,
    memory_id: str,
    project_id: str,
    app_id: str,
) -> str | None:
    try:
        session_factory = client.app.state.session_factory
    except AttributeError:
        return None

    def active_projection() -> MemoryIndex | None:
        with session_factory() as session:
            return session.scalar(
                select(MemoryIndex).where(
                    MemoryIndex.project_id == project_id,
                    MemoryIndex.mem0_memory_id == memory_id,
                    MemoryIndex.deleted_at.is_(None),
                )
            )

    if active_projection() is None:
        return None
    try:
        response = client.post(
            "/v1/memories/query",
            json={"project_id": project_id, "app_id": app_id},
        )
    except Exception as exc:
        return f"Projection cleanup raised {type(exc).__name__}: {exc}"
    if response.status_code != 200:
        return (
            f"Projection cleanup returned HTTP {response.status_code}: "
            f"{response.text}"
        )
    if active_projection() is not None:
        return f"Projection cleanup still found active {memory_id}"
    return None


def _cleanup_memory_fixture(
    client: TestClient,
    settings: SidecarSettings,
    *,
    memory_id: str,
    project_id: str,
    app_id: str,
) -> str | None:
    scoped_error = _delete_scoped_memory(
        client,
        memory_id=memory_id,
        project_id=project_id,
        app_id=app_id,
    )
    absence_error = _upstream_absence_error(settings, memory_id)
    if scoped_error is not None or absence_error is not None:
        direct_error = _delete_direct_upstream_memory(settings, memory_id)
        absence_error = _upstream_absence_error(settings, memory_id)
        if absence_error is not None:
            diagnostics = "; ".join(
                error
                for error in (scoped_error, direct_error, absence_error)
                if error is not None
            )
            return diagnostics
    return _projection_absence_error(
        client,
        memory_id=memory_id,
        project_id=project_id,
        app_id=app_id,
    )


def _cleanup_direct_fixture(
    settings: SidecarSettings,
    memory_id: str,
) -> str | None:
    direct_error = _delete_direct_upstream_memory(settings, memory_id)
    absence_error = _upstream_absence_error(settings, memory_id)
    if absence_error is None:
        return None
    return "; ".join(
        error for error in (direct_error, absence_error) if error is not None
    )


def _report_cleanup_failure(error: str | None) -> None:
    if error is None:
        return
    if sys.exc_info()[0] is None:
        raise AssertionError(error)
    print(error, file=sys.stderr)


def _report_cleanup_failures(errors: list[str | None]) -> None:
    combined = "; ".join(error for error in errors if error is not None)
    _report_cleanup_failure(combined or None)


def test_cleanup_failures_are_aggregated_before_reporting() -> None:
    with pytest.raises(AssertionError) as exc_info:
        _report_cleanup_failures(["first cleanup failed", None, "second failed"])

    assert "first cleanup failed" in str(exc_info.value)
    assert "second failed" in str(exc_info.value)


class PrimaryFixtureFailure(Exception):
    pass


@pytest.mark.parametrize("failure_type", [ValueError, PrimaryFixtureFailure])
def test_cleanup_diagnostics_do_not_mask_active_fixture_failure(
    failure_type,
    capsys,
) -> None:
    primary = failure_type("primary fixture failure")

    with pytest.raises(failure_type) as exc_info:
        try:
            raise primary
        finally:
            _report_cleanup_failures(["first cleanup failed", "second failed"])

    assert exc_info.value is primary
    assert (
        "first cleanup failed; second failed" in capsys.readouterr().err
    )


def test_scoped_cleanup_reports_non_success_response() -> None:
    class FailedCleanupClient:
        def delete(self, *args, **kwargs):
            return SimpleNamespace(status_code=500, text="cleanup failed")

    error = _delete_scoped_memory(
        FailedCleanupClient(),
        memory_id="mem-1",
        project_id="project-a",
        app_id="app-a",
    )

    assert error == "Scoped cleanup returned HTTP 500: cleanup failed"


def test_scoped_cleanup_rejects_404_with_active_projection(tmp_path) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'cleanup.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="project-a",
        ),
        mem0_client=object(),
    )
    with app.state.session_factory() as session:
        session.add(
            MemoryIndex(
                project_id="project-a",
                mem0_memory_id="mem-1",
                app_id="app-a",
            )
        )
        session.commit()

    class MissingCleanupClient:
        def __init__(self):
            self.app = app

        def delete(self, *args, **kwargs):
            return SimpleNamespace(status_code=404, text="not found")

    error = _delete_scoped_memory(
        MissingCleanupClient(),
        memory_id="mem-1",
        project_id="project-a",
        app_id="app-a",
    )

    assert error == "Scoped cleanup returned HTTP 404 with active projection"


def test_direct_cleanup_reports_non_success_response(monkeypatch) -> None:
    monkeypatch.setattr(
        httpx,
        "delete",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=500,
            text="cleanup failed",
        ),
    )

    error = _delete_direct_upstream_memory(
        SidecarSettings(mem0_base_url="http://mem0.local"),
        "mem-1",
    )

    assert error == "Direct upstream cleanup returned HTTP 500: cleanup failed"


def test_fixture_cleanup_falls_back_to_upstream_and_proves_absence(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class MissingScopedClient:
        def delete(self, *args, **kwargs):
            calls.append("scoped")
            return SimpleNamespace(status_code=404, text="not found")

    monkeypatch.setattr(
        httpx,
        "delete",
        lambda *args, **kwargs: (
            calls.append("direct")
            or SimpleNamespace(status_code=200, text="deleted")
        ),
    )
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=200,
            text='{"results": []}',
            json=lambda: {"results": []},
        ),
    )

    error = _cleanup_memory_fixture(
        MissingScopedClient(),
        SidecarSettings(mem0_base_url="http://mem0.local"),
        memory_id="mem-1",
        project_id="project-a",
        app_id="app-a",
    )

    assert error is None
    assert calls == ["scoped", "direct"]


def _wait_for_history_update(
    client: TestClient,
    *,
    memory_id: str,
    project_id: str,
    app_id: str,
    updated_text: str,
    timeout_seconds: float = 15,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last_diagnostic = "history endpoint was not called"
    while time.monotonic() < deadline:
        response = client.get(
            f"/v1/memories/{memory_id}/history",
            params=_scoped_params(project_id=project_id, app_id=app_id),
        )
        if response.status_code == 200:
            payload = response.json()
            if _record_contains(payload.get("results", []), updated_text):
                return payload
            last_diagnostic = f"history had no update: {payload!r}"
        else:
            last_diagnostic = (
                f"history returned HTTP {response.status_code}: {response.text}"
            )
        time.sleep(0.25)
    raise AssertionError(
        f"Timed out waiting for memory history update: {last_diagnostic}"
    )


def test_live_memory_explorer_lifecycle_is_scoped_against_mem0_oss(
    tmp_path,
) -> None:
    token = uuid4().hex
    project_id = f"e2e-project-{token}"
    app_id = f"e2e-app-{token}"
    foreign_app_id = f"e2e-foreign-app-{token}"
    wrong_app_id = f"e2e-wrong-app-{token}"
    user_id = f"e2e-user-{token}"
    agent_id = f"e2e-agent-{token}"
    run_id = f"e2e-run-{token}"
    category_name = f"e2e-category-{token}"
    settings = _live_settings(tmp_path, project_id=project_id)
    client = TestClient(create_app(settings=settings))
    marker = f"sidecar-e2e-{token}"
    trace_secret = f"live-trace-secret-{token}"
    updated_text = f"Updated {marker}"
    memory_id: str | None = None
    foreign_memory_id: str | None = None
    category_id: str | None = None
    created_after = datetime.now(UTC) - timedelta(minutes=1)

    try:
        category_response = client.post(
            f"/v1/projects/{project_id}/categories",
            json={
                "name": category_name,
                "description": "Live memory explorer category",
                "schema": {"type": "object"},
                "enabled": True,
                "strategy": "metadata",
            },
        )
        assert category_response.status_code == 201, category_response.text
        category_id = category_response.json()["id"]

        add_response = client.post(
            "/v3/memories/add/",
            headers={"X-Request-ID": f"live-add-{token}"},
            json={
                "text": f"Remember {marker}",
                "project_id": project_id,
                "user_id": user_id,
                "agent_id": agent_id,
                "app_id": app_id,
                "run_id": run_id,
                "infer": False,
                "metadata": {
                    "category": category_name,
                    "marker": marker,
                    "revision": "before",
                    "token": trace_secret,
                    "internal_url": "http://mem0:8000/private",
                },
            },
        )
        assert add_response.status_code == 200, add_response.text
        add_body = add_response.json()
        memory_ids = _extract_memory_ids(add_body["memory"])
        assert len(memory_ids) == 1, add_body
        memory_id = memory_ids[0]
        assert add_body["event"]["status"] == "SUCCEEDED"

        foreign_add_response = client.post(
            "/v3/memories/add/",
            headers={"X-Request-ID": f"live-add-foreign-{token}"},
            json={
                "text": f"Remember {marker}",
                "project_id": project_id,
                "user_id": user_id,
                "agent_id": agent_id,
                "app_id": foreign_app_id,
                "run_id": run_id,
                "infer": False,
                "metadata": {
                    "category": category_name,
                    "marker": marker,
                    "revision": "foreign",
                },
            },
        )
        assert foreign_add_response.status_code == 200, foreign_add_response.text
        foreign_memory_ids = _extract_memory_ids(
            foreign_add_response.json()["memory"]
        )
        assert len(foreign_memory_ids) == 1, foreign_add_response.json()
        foreign_memory_id = foreign_memory_ids[0]
        assert foreign_memory_id != memory_id

        search_response = client.post(
            "/v3/memories/search/",
            headers={"X-Request-ID": f"live-search-{token}"},
            json={
                "query": marker,
                "project_id": project_id,
                "user_id": user_id,
                "agent_id": agent_id,
                "app_id": app_id,
                "run_id": run_id,
            },
        )
        assert search_response.status_code == 200, search_response.text
        assert memory_id in _extract_memory_ids(search_response.json())

        created_before = datetime.now(UTC) + timedelta(minutes=1)
        query_response = client.post(
            "/v1/memories/query",
            headers={"X-Request-ID": f"live-list-present-{token}"},
            json={
                "project_id": project_id,
                "app_id": app_id,
                "match": "all",
                "filters": [
                    {
                        "field": "entity_type",
                        "operator": "equals",
                        "value": "app",
                    },
                    {
                        "field": "category",
                        "operator": "equals",
                        "value": category_name,
                    },
                    {
                        "field": "metadata",
                        "operator": "contains",
                        "value": {"key": "marker", "value": marker},
                    },
                ],
                "date_range": {
                    "from": created_after.isoformat(),
                    "to": created_before.isoformat(),
                },
            },
        )
        assert query_response.status_code == 200, query_response.text
        query_body = query_response.json()
        assert query_body["stale_skipped"] == 0
        assert [item["id"] for item in query_body["results"]] == [memory_id]
        assert foreign_memory_id not in {
            item["id"] for item in query_body["results"]
        }

        detail_response = client.get(
            f"/v1/memories/{memory_id}",
            params=_scoped_params(project_id=project_id, app_id=app_id),
        )
        assert detail_response.status_code == 200, detail_response.text
        assert detail_response.json()["id"] == memory_id

        patch_response = client.patch(
            f"/v1/memories/{memory_id}",
            params=_scoped_params(project_id=project_id, app_id=app_id),
            json={
                "text": updated_text,
                "metadata": {
                    "category": category_name,
                    "marker": marker,
                    "revision": "after",
                },
                "expiration_date": "2099-12-31",
            },
        )
        assert patch_response.status_code == 200, patch_response.text
        patched = patch_response.json()
        assert patched["event"]["status"] == "SUCCEEDED"
        assert patched["memory"]["memory"] == updated_text
        assert patched["memory"]["metadata"]["revision"] == "after"
        assert patched["memory"]["expiration_date"] == "2099-12-31"

        history = _wait_for_history_update(
            client,
            memory_id=memory_id,
            project_id=project_id,
            app_id=app_id,
            updated_text=updated_text,
        )
        assert _record_contains(history["results"], updated_text)

        wrong_app_query = client.post(
            "/v1/memories/query",
            json={"project_id": project_id, "app_id": wrong_app_id},
        )
        assert wrong_app_query.status_code == 200, wrong_app_query.text
        assert wrong_app_query.json()["results"] == []
        assert wrong_app_query.json()["total"] == 0

        wrong_app_get = client.get(
            f"/v1/memories/{memory_id}",
            params=_scoped_params(
                project_id=project_id,
                app_id=wrong_app_id,
            ),
        )
        assert wrong_app_get.status_code == 404, wrong_app_get.text

        delete_response = client.delete(
            f"/v1/memories/{memory_id}",
            params=_scoped_params(project_id=project_id, app_id=app_id),
        )
        assert delete_response.status_code == 200, delete_response.text
        assert delete_response.json()["event"]["status"] == "SUCCEEDED"

        deleted_query = client.post(
            "/v1/memories/query",
            headers={"X-Request-ID": f"live-list-empty-{token}"},
            json={"project_id": project_id, "app_id": app_id},
        )
        assert deleted_query.status_code == 200, deleted_query.text
        assert all(
            item["id"] != memory_id for item in deleted_query.json()["results"]
        )
        with client.app.state.session_factory() as session:
            projection = session.scalar(
                select(MemoryIndex).where(
                    MemoryIndex.project_id == project_id,
                    MemoryIndex.mem0_memory_id == memory_id,
                )
            )
        assert projection is not None and projection.deleted_at is not None
    finally:
        cleanup_errors: list[str | None] = []
        if memory_id is not None:
            cleanup_errors.append(
                _cleanup_memory_fixture(
                    client,
                    settings,
                    memory_id=memory_id,
                    project_id=project_id,
                    app_id=app_id,
                )
            )
        if foreign_memory_id is not None:
            cleanup_errors.append(
                _cleanup_memory_fixture(
                    client,
                    settings,
                    memory_id=foreign_memory_id,
                    project_id=project_id,
                    app_id=foreign_app_id,
                )
            )
        if category_id is not None:
            try:
                category_cleanup = client.delete(
                    f"/v1/projects/{project_id}/categories/{category_id}"
                )
                if category_cleanup.status_code != 204:
                    cleanup_errors.append(
                        "Category cleanup returned HTTP "
                        f"{category_cleanup.status_code}: {category_cleanup.text}"
                    )
            except Exception as exc:
                cleanup_errors.append(
                    f"Category cleanup raised {type(exc).__name__}: {exc}"
                )
        _report_cleanup_failures(cleanup_errors)

    events_response = client.get("/v1/events")
    assert events_response.status_code == 200, events_response.text
    operations = [event["operation"] for event in events_response.json()["results"]]
    assert "memory.add" in operations
    assert "memory.delete" in operations

    trace_query = client.post(
        "/v1/events/query",
        json={
            "project_id": project_id,
            "app_id": app_id,
            "statuses": ["SUCCEEDED"],
            "page_size": 100,
        },
    )
    assert trace_query.status_code == 200, trace_query.text
    traces = trace_query.json()["results"]
    traced_operations = {
        trace["display_operation"]
        for trace in traces
        if trace["display_operation"] in {"ADD", "SEARCH", "GET ALL"}
    }
    assert traced_operations == {"ADD", "SEARCH", "GET ALL"}
    trace_by_correlation = {trace["correlation_id"]: trace for trace in traces}
    assert f"live-add-foreign-{token}" not in trace_by_correlation
    assert trace_by_correlation[f"live-add-{token}"]["status"] == "SUCCEEDED"
    assert trace_by_correlation[f"live-search-{token}"]["result_count"] >= 1
    assert trace_by_correlation[f"live-list-present-{token}"]["has_results"] is True
    assert trace_by_correlation[f"live-list-empty-{token}"]["has_results"] is False

    search_trace = trace_by_correlation[f"live-search-{token}"]
    detail_response = client.get(
        f"/v1/event/{search_trace['id']}",
        params={"project_id": project_id, "app_id": app_id},
    )
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()
    assert detail["display_operation"] == "SEARCH"
    assert detail["correlation_id"] == f"live-search-{token}"
    assert detail["result_previews"]
    assert {
        preview.get("id") or preview.get("memory_id")
        for preview in detail["result_previews"]
    } == {memory_id}

    list_trace = trace_by_correlation[f"live-list-present-{token}"]
    list_detail_response = client.get(
        f"/v1/event/{list_trace['id']}",
        params={"project_id": project_id, "app_id": app_id},
    )
    assert list_detail_response.status_code == 200, list_detail_response.text
    list_detail = list_detail_response.json()
    assert list_detail["display_operation"] == "GET ALL"
    assert {
        preview.get("id") or preview.get("memory_id")
        for preview in list_detail["result_previews"]
    } == {memory_id}
    assert foreign_memory_id not in {
        preview.get("id") or preview.get("memory_id")
        for preview in list_detail["result_previews"]
    }

    with client.app.state.session_factory() as session:
        raw_events = list(
            session.scalars(select(Event).where(Event.project_id == project_id))
        )
    raw_trace_json = "\n".join(
        document
        for event in raw_events
        for document in (event.request_json, event.response_json, event.error_json)
    )
    assert trace_secret not in raw_trace_json
    assert "http://mem0:8000" not in raw_trace_json
    assert all(
        len(document.encode("utf-8")) <= 65_536
        for event in raw_events
        for document in (event.request_json, event.response_json, event.error_json)
    )


def test_live_reconcile_imports_only_marked_scope(tmp_path) -> None:
    token = uuid4().hex
    project_id = f"reconcile-project-{token}"
    app_id = f"reconcile-app-{token}"
    other_app_id = f"reconcile-other-app-{token}"
    user_id = f"reconcile-user-{token}"
    run_id = f"reconcile-run-{token}"
    marker = f"marked-reconcile-{token}"
    creator_settings = _live_settings(
        tmp_path,
        project_id=project_id,
        database_name="marked-creator.sqlite3",
    )
    target_settings = _live_settings(
        tmp_path,
        project_id=project_id,
        database_name="marked-target.sqlite3",
    )
    creator = TestClient(create_app(settings=creator_settings))
    target = TestClient(create_app(settings=target_settings))
    memory_id: str | None = None
    other_scope_memory_id: str | None = None
    unscoped_memory_id: str | None = None
    cleanup_targets: list[tuple[str, str]] = []
    direct_cleanup_targets: list[str] = []

    try:
        add_response = creator.post(
            "/v3/memories/add/",
            json={
                "project_id": project_id,
                "app_id": app_id,
                "user_id": user_id,
                "run_id": run_id,
                "text": marker,
                "infer": False,
                "metadata": {"marker": marker},
            },
        )
        assert add_response.status_code == 200, add_response.text
        memory_id = add_response.json()["event"]["subject_id"]
        cleanup_targets.append((memory_id, app_id))

        other_scope_response = creator.post(
            "/v3/memories/add/",
            json={
                "project_id": project_id,
                "app_id": other_app_id,
                "user_id": f"other-{user_id}",
                "run_id": f"other-{run_id}",
                "text": f"other-{marker}",
                "infer": False,
                "metadata": {"marker": f"other-{marker}"},
            },
        )
        assert other_scope_response.status_code == 200, other_scope_response.text
        other_scope_memory_id = other_scope_response.json()["event"][
            "subject_id"
        ]
        cleanup_targets.append((other_scope_memory_id, other_app_id))

        unscoped_memory_id = _add_direct_upstream_memory(
            creator_settings,
            text=f"unscoped-{marker}",
            user_id=f"unscoped-{user_id}",
            run_id=f"unscoped-{run_id}",
            metadata={"marker": f"unscoped-{marker}"},
        )
        direct_cleanup_targets.append(unscoped_memory_id)

        reconcile_response = target.post(
            f"/v1/projects/{project_id}/memories/reconcile",
            json={
                "project_id": project_id,
                "app_id": app_id,
                "adopt_unscoped": False,
            },
        )
        assert reconcile_response.status_code == 200, reconcile_response.text
        reconcile = reconcile_response.json()
        assert set(reconcile) == {
            "scanned",
            "indexed",
            "skipped_unscoped",
            "skipped_other_scope",
            "stale_marked",
        }
        assert reconcile["scanned"] == 3
        assert reconcile["indexed"] == 1
        assert reconcile["skipped_unscoped"] == 1
        assert reconcile["skipped_other_scope"] == 1
        assert (
            reconcile["indexed"]
            + reconcile["skipped_unscoped"]
            + reconcile["skipped_other_scope"]
            == reconcile["scanned"]
        )
        assert reconcile["stale_marked"] == 0

        detail_response = target.get(
            f"/v1/memories/{memory_id}",
            params=_scoped_params(project_id=project_id, app_id=app_id),
        )
        assert detail_response.status_code == 200, detail_response.text
        assert detail_response.json()["id"] == memory_id

        with target.app.state.session_factory() as session:
            unexpected_projection_ids = set(
                session.scalars(
                    select(MemoryIndex.mem0_memory_id).where(
                        MemoryIndex.project_id == project_id,
                        MemoryIndex.mem0_memory_id.in_(
                            [other_scope_memory_id, unscoped_memory_id]
                        ),
                        MemoryIndex.deleted_at.is_(None),
                    )
                )
            )
        assert unexpected_projection_ids == set()
    finally:
        cleanup_errors = []
        for cleanup_memory_id, cleanup_app_id in cleanup_targets:
            cleanup_errors.append(
                _cleanup_memory_fixture(
                    creator,
                    creator_settings,
                    memory_id=cleanup_memory_id,
                    project_id=project_id,
                    app_id=cleanup_app_id,
                )
            )
        for cleanup_memory_id in direct_cleanup_targets:
            cleanup_errors.append(
                _cleanup_direct_fixture(creator_settings, cleanup_memory_id)
            )
        if memory_id is not None:
            cleanup_errors.append(
                _projection_absence_error(
                    target,
                    memory_id=memory_id,
                    project_id=project_id,
                    app_id=app_id,
                )
            )
        _report_cleanup_failures(cleanup_errors)


def test_live_reconcile_refuses_unscoped_adoption_without_runtime_gate(
    tmp_path,
) -> None:
    token = uuid4().hex
    project_id = f"refuse-project-{token}"
    app_id = f"refuse-app-{token}"
    settings = _live_settings(tmp_path, project_id=project_id)
    client = TestClient(create_app(settings=settings))
    memory_id = _add_direct_upstream_memory(
        settings,
        text=f"unscoped-refusal-{token}",
        user_id=f"refuse-user-{token}",
        run_id=f"refuse-run-{token}",
        metadata={"e2e_marker": token},
    )

    try:
        response = client.post(
            f"/v1/projects/{project_id}/memories/reconcile",
            json={
                "project_id": project_id,
                "app_id": app_id,
                "adopt_unscoped": True,
            },
        )
        assert response.status_code == 422, response.text
        assert response.json() == {
            "detail": "Unscoped memory adoption is disabled at runtime"
        }
        query_response = client.post(
            "/v1/memories/query",
            json={"project_id": project_id, "app_id": app_id},
        )
        assert query_response.status_code == 200, query_response.text
        assert query_response.json()["results"] == []
    finally:
        _report_cleanup_failure(
            _cleanup_direct_fixture(settings, memory_id)
        )


@pytest.mark.adoption_e2e
def test_live_reconcile_adopts_unscoped_only_in_dedicated_runner(tmp_path) -> None:
    if os.environ.get("MEM0_E2E_ADOPTION_ENABLED") != "true":
        pytest.skip("dedicated unscoped-adoption runner is not enabled")

    token = uuid4().hex
    project_id = f"adopt-project-{token}"
    app_id = f"adopt-app-{token}"
    settings = _live_settings(tmp_path, project_id=project_id)
    assert settings.allow_adopt_unscoped_memories is True
    assert settings.default_project_id == project_id
    client = TestClient(create_app(settings=settings))
    memory_id = _add_direct_upstream_memory(
        settings,
        text=f"unscoped-adoption-{token}",
        user_id=f"adopt-user-{token}",
        run_id=f"adopt-run-{token}",
        metadata={"e2e_marker": token},
    )
    adopted = False

    try:
        response = client.post(
            f"/v1/projects/{project_id}/memories/reconcile",
            json={
                "project_id": project_id,
                "app_id": app_id,
                "adopt_unscoped": True,
            },
        )
        assert response.status_code == 200, response.text
        adopted = True
        counters = response.json()
        assert counters["indexed"] == 1
        assert counters["skipped_unscoped"] == 0

        detail_response = client.get(
            f"/v1/memories/{memory_id}",
            params=_scoped_params(project_id=project_id, app_id=app_id),
        )
        assert detail_response.status_code == 200, detail_response.text
    finally:
        if adopted:
            cleanup_error = _cleanup_memory_fixture(
                client,
                settings,
                memory_id=memory_id,
                project_id=project_id,
                app_id=app_id,
            )
        else:
            cleanup_error = _cleanup_direct_fixture(settings, memory_id)
        _report_cleanup_failure(cleanup_error)


def test_live_categories_and_export_flow(tmp_path) -> None:
    token = uuid4().hex
    project_id = f"overlay-project-{token}"
    app_id = f"overlay-app-{token}"
    other_app_id = f"overlay-other-app-{token}"
    user_id = f"overlay-user-{token}"
    other_user_id = f"overlay-other-user-{token}"
    run_id = f"overlay-run-{token}"
    other_run_id = f"overlay-other-run-{token}"
    category_name = f"preferences-{token}"
    settings = _live_settings(tmp_path, project_id=project_id)
    client = TestClient(create_app(settings=settings))
    marker = f"dashboard overlay export marker {token}"
    out_of_scope_marker = f"dashboard overlay out-of-scope marker {token}"
    cleanup_targets: list[tuple[str, str]] = []
    category_id: str | None = None
    category_schema = {
        "type": "object",
        "properties": {
            "theme": {"type": "string", "enum": ["light", "dark"]},
            "score": {"type": "number"},
            "birthday": {"type": "string", "format": "date"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "profile": {
                "type": "object",
                "properties": {"nickname": {"type": "string"}},
                "required": ["nickname"],
            },
        },
        "required": ["theme"],
    }

    try:
        categories_response = client.post(
            f"/v1/projects/{project_id}/categories",
            json={
                "name": category_name,
                "description": "E2E preferences category",
                "schema": category_schema,
                "enabled": True,
                "strategy": "metadata",
            },
        )
        assert categories_response.status_code == 201, categories_response.text
        category = categories_response.json()
        category_id = category["id"]
        assert category["schema"] == category_schema

        patch_response = client.patch(
            f"/v1/projects/{project_id}/categories/{category_id}",
            json={"description": "Updated E2E preferences category"},
        )
        assert patch_response.status_code == 200, patch_response.text
        assert (
            patch_response.json()["description"] == "Updated E2E preferences category"
        )
        assert patch_response.json()["version"] == 2

        add_response = client.post(
            "/v3/memories/add/",
            json={
                "project_id": project_id,
                "app_id": app_id,
                "user_id": user_id,
                "run_id": run_id,
                "infer": False,
                "metadata": {"category": category_name},
                "messages": [{"role": "user", "content": marker}],
            },
        )
        assert add_response.status_code == 200, add_response.text
        memory_id = add_response.json()["event"]["subject_id"]
        cleanup_targets.append((memory_id, app_id))
        with client.app.state.session_factory() as session:
            indexed_memory = session.scalar(
                select(MemoryIndex).where(
                    MemoryIndex.project_id == project_id,
                    MemoryIndex.mem0_memory_id == memory_id,
                    MemoryIndex.app_id == app_id,
                )
            )
        assert indexed_memory is not None
        assert indexed_memory.category == category_name

        out_of_scope_response = client.post(
            "/v3/memories/add/",
            json={
                "project_id": project_id,
                "app_id": other_app_id,
                "user_id": other_user_id,
                "run_id": other_run_id,
                "infer": False,
                "metadata": {"category": category_name},
                "messages": [{"role": "user", "content": out_of_scope_marker}],
            },
        )
        assert out_of_scope_response.status_code == 200, out_of_scope_response.text
        out_of_scope_memory_id = out_of_scope_response.json()["event"]["subject_id"]
        cleanup_targets.append((out_of_scope_memory_id, other_app_id))

        export_response = client.post(
            "/v1/exports",
            json={
                "project_id": project_id,
                "format": "json",
                "filters": {"app_id": app_id, "user_id": user_id},
            },
        )
        assert export_response.status_code == 200, export_response.text
        job = export_response.json()
        assert job["status"] == "SUCCEEDED"
        assert job["exported_count"] >= 1

        download_response = client.get(
            f"/v1/exports/{job['id']}/download",
            params={"project_id": project_id},
        )
        assert download_response.status_code == 200, download_response.text
        payload = download_response.json()
        assert any(marker in str(memory) for memory in payload["memories"])
        assert all(
            out_of_scope_marker not in str(memory) for memory in payload["memories"]
        )

        disable_response = client.patch(
            f"/v1/projects/{project_id}/categories/{category_id}",
            json={"enabled": False},
        )
        assert disable_response.status_code == 200, disable_response.text
        assert disable_response.json()["enabled"] is False
        assert disable_response.json()["version"] == 3
    finally:
        cleanup_errors = []
        for cleanup_memory_id, cleanup_app_id in cleanup_targets:
            cleanup_errors.append(
                _cleanup_memory_fixture(
                    client,
                    settings,
                    memory_id=cleanup_memory_id,
                    project_id=project_id,
                    app_id=cleanup_app_id,
                )
            )
        if category_id is not None:
            try:
                delete_category_response = client.delete(
                    f"/v1/projects/{project_id}/categories/{category_id}"
                )
                if delete_category_response.status_code != 204:
                    cleanup_errors.append(
                        "Category cleanup returned HTTP "
                        f"{delete_category_response.status_code}: "
                        f"{delete_category_response.text}"
                    )
            except Exception as exc:
                cleanup_errors.append(
                    f"Category cleanup raised {type(exc).__name__}: {exc}"
                )
        _report_cleanup_failures(cleanup_errors)
