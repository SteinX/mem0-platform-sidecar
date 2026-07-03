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


@pytest.mark.asyncio
async def test_mem0_client_posts_search_memory_payload() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/memories/search/"
        assert request.headers["authorization"] == "Bearer local-key"
        assert request.read() == b'{"query":"hello","user_id":"root"}'
        return httpx.Response(200, json={"results": [{"id": "mem-1"}]})

    transport = httpx.MockTransport(handler)
    client = Mem0RestClient(
        base_url="http://mem0.local",
        api_key="local-key",
        transport=transport,
    )

    result = await client.search_memories({"query": "hello", "user_id": "root"})

    assert result == {"results": [{"id": "mem-1"}]}


@pytest.mark.asyncio
async def test_mem0_client_gets_memory_by_id() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/memories/mem-1/"
        return httpx.Response(200, json={"id": "mem-1", "memory": "hello"})

    client = Mem0RestClient(
        base_url="http://mem0.local",
        transport=httpx.MockTransport(handler),
    )

    result = await client.get_memory("mem-1")

    assert result == {"id": "mem-1", "memory": "hello"}


@pytest.mark.asyncio
async def test_mem0_client_deletes_memory_by_id() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/memories/mem-1/"
        return httpx.Response(200, json={"message": "Deleted"})

    client = Mem0RestClient(
        base_url="http://mem0.local",
        transport=httpx.MockTransport(handler),
    )

    result = await client.delete_memory("mem-1")

    assert result == {"message": "Deleted"}
