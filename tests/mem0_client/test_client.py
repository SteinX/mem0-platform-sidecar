import httpx
import pytest

from mem0_sidecar.mem0_client import client as client_module
from mem0_sidecar.mem0_client.client import Mem0RestClient, Mem0UpstreamError


def _bounded_value_contains_secret(value: object, secret: str) -> bool:
    pending: list[tuple[object, int]] = [(value, 0)]
    seen: set[int] = set()
    secret_bytes = secret.encode()
    visited = 0
    while pending and visited < 256:
        current, depth = pending.pop()
        current_id = id(current)
        if current_id in seen:
            continue
        seen.add(current_id)
        visited += 1
        if type(current) is str:
            if secret in current:
                return True
            continue
        if type(current) is bytes:
            if secret_bytes in current:
                return True
            continue
        if depth >= 4:
            continue
        if type(current) is dict:
            for key, item in list(current.items())[:32]:
                pending.extend(((key, depth + 1), (item, depth + 1)))
        elif type(current) in {list, tuple, set, frozenset}:
            pending.extend((item, depth + 1) for item in list(current)[:32])
    return False


def _linked_exception_graph_contains_secret(
    error: BaseException,
    secret: str,
) -> bool:
    pending: list[tuple[BaseException, int]] = [
        (linked, 0)
        for linked in (error.__cause__, error.__context__)
        if linked is not None
    ]
    seen: set[int] = set()
    visited = 0
    while pending and visited < 16:
        current, depth = pending.pop()
        current_id = id(current)
        if current_id in seen:
            continue
        seen.add(current_id)
        visited += 1
        if _bounded_value_contains_secret((current.args, vars(current)), secret):
            return True

        traceback = current.__traceback__
        frame_count = 0
        while traceback is not None and frame_count < 32:
            local_values = list(traceback.tb_frame.f_locals.values())[:64]
            if _bounded_value_contains_secret(local_values, secret):
                return True
            traceback = traceback.tb_next
            frame_count += 1

        if depth < 8:
            pending.extend(
                (linked, depth + 1)
                for linked in (current.__cause__, current.__context__)
                if linked is not None
            )
    return False


def _decoder_graph_state(
    error: BaseException,
    secret: str,
) -> tuple[str | None, str | None, bool]:
    return (
        type(error.__cause__).__name__ if error.__cause__ is not None else None,
        type(error.__context__).__name__ if error.__context__ is not None else None,
        _linked_exception_graph_contains_secret(error, secret),
    )


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
async def test_mem0_client_normalizes_nested_search_hits_and_preserves_score() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "memory": {"id": "mem-1", "memory": "hello"},
                        "score": 0.94,
                    }
                ]
            },
        )

    client = Mem0RestClient(
        base_url="http://mem0.local",
        transport=httpx.MockTransport(handler),
    )

    result = await client.search_memories({"query": "hello"})

    assert result == {
        "results": [{"id": "mem-1", "memory": "hello", "score": 0.94}],
        "total": 1,
    }


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
async def test_mem0_client_normalizes_null_memory_to_not_found() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/memories/missing"
        return httpx.Response(
            200,
            content=b"null",
            headers={"content-type": "application/json"},
        )

    client = Mem0RestClient(
        base_url="http://mem0.local",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(Mem0UpstreamError) as error_info:
        await client.get_memory("missing")

    assert error_info.value.status_code == 404
    assert error_info.value.outcome_unknown is False


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
@pytest.mark.parametrize("operation", ["get", "update", "delete"])
@pytest.mark.parametrize(
    ("memory_id", "encoded_id"),
    [
        ("part/what?#%é", "part%2Fwhat%3F%23%25%C3%A9"),
        ("literal%2Fslash", "literal%252Fslash"),
    ],
)
async def test_mem0_client_exact_memory_routes_quote_id_once_as_one_path_segment(
    operation: str,
    memory_id: str,
    encoded_id: str,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == f"/memories/{encoded_id}".encode()
        assert request.url.query == b""
        assert request.url.fragment == ""
        return httpx.Response(200, json={"id": memory_id})

    client = Mem0RestClient(
        base_url="http://mem0.local",
        transport=httpx.MockTransport(handler),
    )

    if operation == "get":
        result = await client.get_memory(memory_id)
    elif operation == "update":
        result = await client.update_memory(memory_id, {"text": "updated"})
    else:
        result = await client.delete_memory(memory_id)

    assert result == {"id": memory_id}


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
    assert error.outcome_unknown is False
    assert any(
        record.message == "mem0_upstream_request_failed"
        and record.method == "POST"
        and record.path == "/search"
        and record.status_code == 503
        and record.error_type == "HTTPStatusError"
        for record in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport_error",
    [
        httpx.ReadTimeout("response timed out"),
        httpx.RemoteProtocolError("peer disconnected before response"),
    ],
)
async def test_mem0_client_classifies_statusless_response_loss_as_ambiguous(
    transport_error: httpx.RequestError,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        transport_error.request = request
        raise transport_error

    client = Mem0RestClient(
        base_url="http://mem0.local",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(client_module.Mem0UpstreamError) as exc_info:
        await client.delete_memory("mem-1")

    error = exc_info.value
    assert error.status_code is None
    assert error.outcome_unknown is True


@pytest.mark.asyncio
async def test_mem0_client_wraps_2xx_invalid_json_as_ambiguous_without_body_leak(
) -> None:
    secret_body = "not-json sk-response-body-secret"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=secret_body,
            headers={"content-type": "application/json"},
        )

    client = Mem0RestClient(
        base_url="http://mem0.local",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(client_module.Mem0UpstreamError) as exc_info:
        await client.update_memory("mem-1", {"text": "updated"})

    error = exc_info.value
    assert error.status_code == 200
    assert error.outcome_unknown is True
    assert error.response_text is None
    assert secret_body not in str(error)
    assert _decoder_graph_state(error, secret_body) == (None, None, False)


@pytest.mark.asyncio
async def test_mem0_client_wraps_deep_valid_2xx_json_exception_without_body_leak(
) -> None:
    secret_body = "sk-deep-response-body-secret"
    deep_json = ("[" * 20_000 + f'"{secret_body}"' + "]" * 20_000).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=deep_json,
            headers={"content-type": "application/json"},
        )

    client = Mem0RestClient(
        base_url="http://mem0.local",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(client_module.Mem0UpstreamError) as exc_info:
        await client.update_memory("mem-1", {"text": "updated"})

    error = exc_info.value
    assert error.status_code == 200
    assert error.outcome_unknown is True
    assert error.response_text is None
    assert secret_body not in str(error)
    assert _decoder_graph_state(error, secret_body) == (None, None, False)
