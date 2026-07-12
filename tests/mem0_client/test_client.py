import httpx
import pytest

from mem0_sidecar.mem0_client import client as client_module
from mem0_sidecar.mem0_client.client import Mem0RestClient


@pytest.mark.asyncio
async def test_mem0_client_posts_add_memory_payload_with_text_translation() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/memories"
        assert request.headers["x-api-key"] == "local-key"
        assert request.read() == (
            b'{"text":"hello","user_id":"root","metadata":{"source":"route"},'
            b'"messages":[{"role":"user","content":"hello"}]}'
        )
        return httpx.Response(200, json={"id": "mem-1"})

    transport = httpx.MockTransport(handler)
    client = Mem0RestClient(
        base_url="http://mem0.local",
        api_key="local-key",
        transport=transport,
    )

    result = await client.add_memory(
        {
            "text": "hello",
            "user_id": "root",
            "metadata": {"source": "route"},
        }
    )

    assert result == {"id": "mem-1"}


@pytest.mark.asyncio
async def test_mem0_client_posts_add_memory_payload_with_memory_fallback() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/memories"
        assert request.headers["x-api-key"] == "local-key"
        assert request.read() == (
            b'{"memory":"remember this","scope":"project",'
            b'"messages":[{"role":"user","content":"remember this"}]}'
        )
        return httpx.Response(200, json={"id": "mem-2"})

    transport = httpx.MockTransport(handler)
    client = Mem0RestClient(
        base_url="http://mem0.local",
        api_key="local-key",
        transport=transport,
    )

    result = await client.add_memory({"memory": "remember this", "scope": "project"})

    assert result == {"id": "mem-2"}


@pytest.mark.asyncio
async def test_mem0_client_posts_search_memory_payload() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        assert request.headers["x-api-key"] == "local-key"
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
        assert request.url.path == "/memories/mem-1"
        return httpx.Response(200, json={"id": "mem-1", "memory": "hello"})

    client = Mem0RestClient(
        base_url="http://mem0.local",
        transport=httpx.MockTransport(handler),
    )

    result = await client.get_memory("mem-1")

    assert result == {"id": "mem-1", "memory": "hello"}


@pytest.mark.asyncio
async def test_mem0_client_lists_memories_with_query_params() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/memories"
        assert request.url.params["top_k"] == "1000"
        assert request.url.params["show_expired"] == "true"
        return httpx.Response(200, json={"results": [{"id": "mem-1"}]})

    client = Mem0RestClient(
        base_url="http://mem0.local",
        transport=httpx.MockTransport(handler),
    )

    result = await client.list_memories({"top_k": 1000, "show_expired": True})

    assert result == {"results": [{"id": "mem-1"}]}


@pytest.mark.asyncio
async def test_mem0_client_normalizes_non_dict_list_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "mem-1"}])

    client = Mem0RestClient(
        base_url="http://mem0.local",
        transport=httpx.MockTransport(handler),
    )

    result = await client.list_memories({})

    assert result == {"results": [{"id": "mem-1"}]}


@pytest.mark.asyncio
async def test_mem0_client_gets_memory_history_with_url_safe_id() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.raw_path == b"/memories/mem%2Fa/history"
        return httpx.Response(200, json={"results": [{"id": "event-1"}]})

    client = Mem0RestClient(
        base_url="http://mem0.local",
        transport=httpx.MockTransport(handler),
    )

    result = await client.get_memory_history("mem/a")

    assert result == {"results": [{"id": "event-1"}]}


@pytest.mark.asyncio
async def test_mem0_client_normalizes_non_dict_history_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "event-1"}])

    client = Mem0RestClient(
        base_url="http://mem0.local",
        transport=httpx.MockTransport(handler),
    )

    result = await client.get_memory_history("mem-1")

    assert result == {"results": [{"id": "event-1"}]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected_path"),
    [
        ("list", "/memories"),
        ("history", "/memories/mem%2Fa/history"),
    ],
)
async def test_mem0_client_list_and_history_errors_retain_request_context(
    operation: str,
    expected_path: str,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="backend warming up")

    client = Mem0RestClient(
        base_url="http://mem0.local",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(client_module.Mem0UpstreamError) as exc_info:
        if operation == "list":
            await client.list_memories({"top_k": 1000})
        else:
            await client.get_memory_history("mem/a")

    error = exc_info.value
    assert error.method == "GET"
    assert error.path == expected_path
    assert error.status_code == 503


@pytest.mark.asyncio
async def test_mem0_client_deletes_memory_by_id() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/memories/mem-1"
        return httpx.Response(200, json={"message": "Deleted"})

    client = Mem0RestClient(
        base_url="http://mem0.local",
        transport=httpx.MockTransport(handler),
    )

    result = await client.delete_memory("mem-1")

    assert result == {"message": "Deleted"}


@pytest.mark.asyncio
async def test_mem0_client_updates_memory_by_id() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/memories/mem-1"
        assert request.read() == b'{"text":"updated","metadata":{"source":"route"}}'
        return httpx.Response(
            200,
            json={"id": "mem-1", "memory": "updated", "metadata": {"source": "route"}},
        )

    client = Mem0RestClient(
        base_url="http://mem0.local",
        transport=httpx.MockTransport(handler),
    )

    result = await client.update_memory(
        "mem-1",
        {"text": "updated", "metadata": {"source": "route"}},
    )

    assert result == {
        "id": "mem-1",
        "memory": "updated",
        "metadata": {"source": "route"},
    }


@pytest.mark.asyncio
async def test_mem0_client_deletes_all_memories() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/memories"
        assert request.url.params == httpx.QueryParams({"user_id": "root"})
        return httpx.Response(200, json={"message": "Deleted"})

    client = Mem0RestClient(
        base_url="http://mem0.local",
        transport=httpx.MockTransport(handler),
    )

    result = await client.delete_all_memories({"user_id": "root"})

    assert result == {"message": "Deleted"}


@pytest.mark.asyncio
async def test_mem0_client_uses_configurable_auth_headers_and_paths() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/search"
        assert request.headers["authorization"] == "Bearer local-key"
        assert request.headers["x-mem0-org"] == "local-org"
        return httpx.Response(200, json={"results": []})

    client = Mem0RestClient(
        base_url="http://mem0.local/api/v1",
        api_key="local-key",
        api_key_header_name="Authorization",
        api_key_prefix="Bearer",
        extra_headers={"X-Mem0-Org": "local-org"},
        search_path="/search",
        transport=httpx.MockTransport(handler),
    )

    result = await client.search_memories({"query": "hello"})

    assert result == {"results": []}


@pytest.mark.asyncio
async def test_mem0_client_logs_and_raises_upstream_error(caplog) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="backend warming up")

    client = Mem0RestClient(
        base_url="http://mem0.local",
        transport=httpx.MockTransport(handler),
    )
    assert hasattr(client_module, "Mem0UpstreamError")

    with caplog.at_level("INFO", logger="mem0_sidecar.mem0_client"):
        with pytest.raises(client_module.Mem0UpstreamError) as exc_info:
            await client.search_memories({"query": "hello"})

    error = exc_info.value
    assert error.method == "POST"
    assert error.path == "/search"
    assert error.status_code == 503
    assert error.response_text == "backend warming up"
    assert any(
        record.message == "mem0_upstream_request_failed"
        and record.method == "POST"
        and record.path == "/search"
        and record.status_code == 503
        and record.error_type == "HTTPStatusError"
        for record in caplog.records
    )
