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


def test_live_sidecar_add_search_get_delete_against_mem0_oss(tmp_path) -> None:
    settings = _live_settings(tmp_path)
    client = TestClient(create_app(settings=settings))
    marker = f"sidecar-e2e-{uuid4()}"

    add_response = client.post(
        "/v3/memories/add/",
        json={
            "text": f"Remember {marker}",
            "user_id": "sidecar-e2e-user",
            "app_id": settings.default_project_id,
            "metadata": {"type": "e2e", "marker": marker},
        },
    )
    assert add_response.status_code == 200, add_response.text
    add_body = add_response.json()
    memory_id = add_body["memory"].get("id") or add_body["memory"].get("memory_id")
    assert memory_id
    assert add_body["event"]["status"] == "SUCCEEDED"

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
    assert memory_id in str(search_body) or marker in str(search_body)

    get_response = client.get(f"/v1/memories/{memory_id}/")
    assert get_response.status_code == 200, get_response.text
    assert get_response.json().get("id") == memory_id

    delete_response = client.delete(f"/v1/memories/{memory_id}/")
    assert delete_response.status_code == 200, delete_response.text
    assert delete_response.json()["event"]["status"] == "SUCCEEDED"

    events_response = client.get("/v1/events")
    assert events_response.status_code == 200, events_response.text
    operations = [event["operation"] for event in events_response.json()["results"]]
    assert "memory.add" in operations
    assert "memory.delete" in operations
