import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app

pytestmark = pytest.mark.e2e


def _live_settings(tmp_path) -> SidecarSettings:
    base_url = os.environ.get("MEM0_E2E_BASE_URL")
    if not base_url:
        pytest.skip("MEM0_E2E_BASE_URL is not set")
    project_id = os.environ.get("MEM0_E2E_PROJECT_ID", "sidecar-e2e")
    return SidecarSettings(
        database_url=f"sqlite:///{tmp_path / 'sidecar-e2e.sqlite3'}",
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


def test_live_sidecar_add_search_get_delete_against_mem0_oss(tmp_path) -> None:
    settings = _live_settings(tmp_path)
    client = TestClient(create_app(settings=settings))
    marker = f"sidecar-e2e-{uuid4()}"
    add_body: dict[str, object] | None = None
    memory_ids: list[str] = []
    cleanup_memory_ids: set[str] = set()

    try:
        add_response = client.post(
            "/v3/memories/add/",
            json={
                "text": f"Remember {marker}",
                "user_id": "sidecar-e2e-user",
                "app_id": settings.default_project_id,
                "infer": False,
                "metadata": {"type": "e2e", "marker": marker},
            },
        )
        assert add_response.status_code == 200, add_response.text
        add_body = add_response.json()
        memory_ids = _extract_memory_ids(add_body["memory"])
        assert memory_ids, add_body
        assert add_body["event"]["status"] == "SUCCEEDED"
        cleanup_memory_ids = set(memory_ids)

        search_response = client.post(
            "/v3/memories/search/",
            json={
                "query": marker,
                "user_id": "sidecar-e2e-user",
                "app_id": settings.default_project_id,
            },
        )
        assert search_response.status_code == 200, search_response.text
        search_body = search_response.json()
        results = search_body["results"]
        assert any(
            any(_record_contains(result, memory_id) for memory_id in memory_ids)
            or _record_contains(result, marker)
            for result in results
        ), search_body

        get_response = client.get(f"/v1/memories/{memory_ids[0]}/")
        assert get_response.status_code == 200, get_response.text
        assert get_response.json().get("id") == memory_ids[0]

        for memory_id in memory_ids:
            delete_response = client.delete(f"/v1/memories/{memory_id}/")
            assert delete_response.status_code == 200, delete_response.text
            assert delete_response.json()["event"]["status"] == "SUCCEEDED"
            cleanup_memory_ids.discard(memory_id)
    finally:
        if add_body is not None:
            for memory_id in cleanup_memory_ids:
                try:
                    client.delete(f"/v1/memories/{memory_id}/")
                except Exception:
                    pass

    events_response = client.get("/v1/events")
    assert events_response.status_code == 200, events_response.text
    operations = [event["operation"] for event in events_response.json()["results"]]
    assert "memory.add" in operations
    assert "memory.delete" in operations


def test_live_categories_and_export_flow(tmp_path) -> None:
    settings = _live_settings(tmp_path)
    client = TestClient(create_app(settings=settings))
    project_id = "e2e-dashboard-overlay"
    app_id = "dashboard-overlay"
    user_id = "root"
    marker = f"dashboard overlay export marker {uuid4()}"
    out_of_scope_marker = f"dashboard overlay out-of-scope marker {uuid4()}"
    cleanup_targets: list[tuple[str, str]] = []

    categories_response = client.put(
        f"/v1/projects/{project_id}/categories",
        json={
            "categories": [
                {
                    "name": "preferences",
                    "description": "E2E preferences category",
                    "schema": {},
                    "enabled": True,
                    "strategy": "metadata",
                }
            ]
        },
    )
    assert categories_response.status_code == 200, categories_response.text
    assert categories_response.json()["categories"][0]["name"] == "preferences"

    add_response = client.post(
        "/v3/memories/add/",
        json={
            "project_id": project_id,
            "app_id": app_id,
            "user_id": user_id,
            "infer": False,
            "metadata": {"category": "preferences"},
            "messages": [{"role": "user", "content": marker}],
        },
    )
    assert add_response.status_code == 200, add_response.text
    memory_id = add_response.json()["event"]["subject_id"]
    cleanup_targets.append((memory_id, app_id))

    out_of_scope_response = client.post(
        "/v3/memories/add/",
        json={
            "project_id": project_id,
            "app_id": f"{app_id}-other",
            "user_id": f"{user_id}-other",
            "infer": False,
            "metadata": {"category": "preferences"},
            "messages": [{"role": "user", "content": out_of_scope_marker}],
        },
    )
    assert out_of_scope_response.status_code == 200, out_of_scope_response.text
    out_of_scope_memory_id = out_of_scope_response.json()["event"]["subject_id"]
    cleanup_targets.append((out_of_scope_memory_id, f"{app_id}-other"))

    try:
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
    finally:
        for cleanup_memory_id, cleanup_app_id in cleanup_targets:
            delete_response = client.delete(
                f"/v1/memories/{cleanup_memory_id}",
                params={"project_id": project_id, "app_id": cleanup_app_id},
            )
            assert delete_response.status_code == 200, delete_response.text
