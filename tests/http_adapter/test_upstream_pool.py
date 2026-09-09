from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.mem0_client.client import Mem0RestClient


def test_upstream_connections_live_until_app_shutdown(tmp_path: Path) -> None:
    class Transport(httpx.MockTransport):
        closes = 0

        async def aclose(self) -> None:
            self.closes += 1
            await super().aclose()

    transport = Transport(lambda request: httpx.Response(200, json={"results": []}))
    upstream = Mem0RestClient(base_url="http://upstream.test", transport=transport)
    app = create_app(
        settings=SidecarSettings(database_url=f"sqlite:///{tmp_path / 'db.sqlite3'}"),
        mem0_client=upstream,
    )
    with TestClient(app) as client:
        for _ in range(2):
            response = client.post(
                "/v3/memories/search",
                json={"project_id": "default", "app_id": "test", "query": "tea"},
            )
            assert response.status_code == 200, response.text
        assert transport.closes == 0
    assert transport.closes == 1
