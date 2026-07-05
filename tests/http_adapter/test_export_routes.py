from typing import Any

from fastapi.testclient import TestClient

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.store.repositories import ExportJobRepository, MemoryIndexRepository


class ExportFakeMem0Client:
    def __init__(self) -> None:
        self.memories_by_id: dict[str, dict[str, Any]] = {}

    async def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "mem-1", "memory": payload.get("text", "")}

    async def search_memories(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"results": []}

    async def get_memory(self, memory_id: str) -> dict[str, Any]:
        return self.memories_by_id[memory_id]

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        return {"message": f"Deleted {memory_id}"}


def _app(tmp_path, mem0: ExportFakeMem0Client):
    return create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="default",
        ),
        mem0_client=mem0,
    )


def test_export_routes_create_list_get_and_download(tmp_path):
    fake_mem0_client = ExportFakeMem0Client()
    fake_mem0_client.memories_by_id = {
        "mem-a": {"id": "mem-a", "memory": "User likes dark mode"}
    }
    app = _app(tmp_path, fake_mem0_client)
    with app.state.session_factory() as session:
        MemoryIndexRepository(session).upsert_memory(
            project_id="default",
            mem0_memory_id="mem-a",
            user_id="root",
            app_id="codex",
            agent_id=None,
            run_id=None,
            category=None,
            metadata={},
        )
        session.commit()

    client = TestClient(app)
    create_response = client.post(
        "/v1/exports",
        json={
            "project_id": "default",
            "format": "json",
            "filters": {"user_id": "root", "app_id": "codex"},
        },
    )
    assert create_response.status_code == 200
    job = create_response.json()
    assert job["status"] == "SUCCEEDED"
    assert job["exported_count"] == 1

    list_response = client.get("/v1/exports", params={"project_id": "default"})
    assert list_response.status_code == 200
    assert list_response.json()["results"][0]["id"] == job["id"]

    get_response = client.get(
        f"/v1/exports/{job['id']}",
        params={"project_id": "default"},
    )
    assert get_response.status_code == 200
    assert get_response.json()["id"] == job["id"]

    download_response = client.get(
        f"/v1/exports/{job['id']}/download",
        params={"project_id": "default"},
    )
    assert download_response.status_code == 200
    assert download_response.json()["memories"][0]["id"] == "mem-a"


def test_export_routes_reject_unsupported_format(tmp_path):
    app = _app(tmp_path, ExportFakeMem0Client())
    client = TestClient(app)

    response = client.post(
        "/v1/exports",
        json={"project_id": "default", "format": "csv", "filters": {}},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Only json export format is supported"


def test_export_routes_reject_non_object_filters(tmp_path):
    app = _app(tmp_path, ExportFakeMem0Client())
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/v1/exports",
        json={"project_id": "default", "format": "json", "filters": ["not-object"]},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Export filters must be a JSON object"


def test_export_routes_return_404_for_missing_job(tmp_path):
    app = _app(tmp_path, ExportFakeMem0Client())
    client = TestClient(app)

    response = client.get("/v1/exports/missing-job", params={"project_id": "default"})

    assert response.status_code == 404
    assert response.json()["detail"] == "Export not found"


def test_export_routes_return_409_when_downloading_before_completion(tmp_path):
    app = _app(tmp_path, ExportFakeMem0Client())
    with app.state.session_factory() as session:
        job = ExportJobRepository(session).create(
            project_id="default",
            export_format="json",
            filters={},
        )
        ExportJobRepository(session).mark_running("default", job.id)
        session.commit()

    client = TestClient(app)
    response = client.get(
        f"/v1/exports/{job.id}/download",
        params={"project_id": "default"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Export is not complete"


def test_export_routes_isolate_access_by_project(tmp_path):
    app = _app(tmp_path, ExportFakeMem0Client())
    with app.state.session_factory() as session:
        job = ExportJobRepository(session).create(
            project_id="alpha",
            export_format="json",
            filters={},
        )
        session.commit()

    client = TestClient(app)
    response = client.get(f"/v1/exports/{job.id}", params={"project_id": "beta"})

    assert response.status_code == 404
    assert response.json()["detail"] == "Export not found"
