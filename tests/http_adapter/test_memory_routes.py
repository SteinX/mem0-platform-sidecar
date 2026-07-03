from typing import Any

from fastapi.testclient import TestClient

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app


class FakeMem0Client:
    def __init__(self) -> None:
        self.add_payloads: list[dict[str, Any]] = []
        self.search_payloads: list[dict[str, Any]] = []
        self.get_memory_ids: list[str] = []
        self.deleted_ids: list[str] = []

    async def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.add_payloads.append(payload)
        return {"id": "mem-1", "memory": payload["text"]}

    async def search_memories(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.search_payloads.append(payload)
        return {"results": [{"id": "mem-1", "memory": "hello"}]}

    async def get_memory(self, memory_id: str) -> dict[str, Any]:
        self.get_memory_ids.append(memory_id)
        return {"id": memory_id, "memory": "hello"}

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        self.deleted_ids.append(memory_id)
        return {"message": f"Deleted {memory_id}"}


def test_memory_routes_round_trip_with_fake_upstream(tmp_path) -> None:
    mem0 = FakeMem0Client()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            default_project_id="repo-a",
        ),
        mem0_client=mem0,
    )
    client = TestClient(app)

    add_response = client.post(
        "/v3/memories/add/",
        json={
            "text": "hello",
            "user_id": "root",
            "metadata": {"type": "decision"},
        },
    )
    assert add_response.status_code == 200
    add_body = add_response.json()
    assert add_body["memory"]["id"] == "mem-1"
    assert add_body["event"]["status"] == "SUCCEEDED"
    assert mem0.add_payloads[0]["app_id"] == "repo-a"

    search_response = client.post(
        "/v3/memories/search/",
        json={"query": "hello", "user_id": "root"},
    )
    assert search_response.status_code == 200
    assert search_response.json()["results"][0]["id"] == "mem-1"

    get_response = client.get("/v1/memories/mem-1/")
    assert get_response.status_code == 200
    assert get_response.json()["id"] == "mem-1"
    assert mem0.get_memory_ids == ["mem-1"]

    delete_response = client.delete("/v1/memories/mem-1/")
    assert delete_response.status_code == 200
    assert delete_response.json()["memory"]["message"] == "Deleted mem-1"
    assert mem0.deleted_ids == ["mem-1"]

    events_response = client.get("/v1/events")
    assert events_response.status_code == 200
    assert len(events_response.json()["results"]) >= 2

    event_id = add_body["event"]["id"]
    event_response = client.get(f"/v1/event/{event_id}")
    assert event_response.status_code == 200
    assert event_response.json()["id"] == event_id
