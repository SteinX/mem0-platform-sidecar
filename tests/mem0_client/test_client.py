import httpx
import pytest

from mem0_sidecar.mem0_client.client import Mem0RestClient


@pytest.mark.asyncio
async def test_mem0_client_posts_add_memory_payload() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/memories/"
        assert request.headers["authorization"] == "Bearer local-key"
        assert request.read() == b'{"text":"hello"}'
        return httpx.Response(200, json={"id": "mem-1"})

    transport = httpx.MockTransport(handler)
    client = Mem0RestClient(
        base_url="http://mem0.local",
        api_key="local-key",
        transport=transport,
    )

    result = await client.add_memory({"text": "hello"})

    assert result == {"id": "mem-1"}
