import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

from mem0_sidecar.observability import get_request_id

LOGGER = logging.getLogger("mem0_sidecar.mem0_client")


class Mem0UpstreamError(RuntimeError):
    def __init__(
        self,
        *,
        method: str,
        path: str,
        message: str,
        status_code: int | None = None,
        response_text: str | None = None,
        outcome_unknown: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.path = path
        self.status_code = status_code
        self.response_text = response_text
        self.outcome_unknown = (
            status_code is None if outcome_unknown is None else outcome_unknown
        )


class Mem0RestClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        api_key_header_name: str = "X-API-Key",
        api_key_prefix: str = "",
        extra_headers: dict[str, str] | None = None,
        request_timeout_seconds: float = 30.0,
        connect_timeout_seconds: float | None = None,
        verify_tls: bool = True,
        ca_bundle: str | None = None,
        memories_path: str = "/memories",
        search_path: str = "/search",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_key_header_name = api_key_header_name
        self.api_key_prefix = api_key_prefix.strip()
        self.extra_headers = dict(extra_headers or {})
        self.request_timeout_seconds = request_timeout_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self.verify_tls = verify_tls
        self.ca_bundle = ca_bundle
        self.memories_path = memories_path
        self.search_path = search_path
        self.transport = transport

    def _headers(self) -> dict[str, str]:
        headers = dict(self.extra_headers)
        if not self.api_key:
            return headers
        if self.api_key_prefix:
            headers[self.api_key_header_name] = f"{self.api_key_prefix} {self.api_key}"
        else:
            headers[self.api_key_header_name] = self.api_key
        return headers

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            self.request_timeout_seconds,
            connect=self.connect_timeout_seconds or self.request_timeout_seconds,
        )

    def _verify(self) -> bool | str:
        return self.ca_bundle or self.verify_tls

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _memory_item_path(self, memory_id: str, *, suffix: str = "") -> str:
        return f"{self.memories_path}/{quote(memory_id, safe='')}{suffix}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self._url(path)
        started_at = time.perf_counter()
        async with httpx.AsyncClient(
            headers=self._headers(),
            transport=self.transport,
            timeout=self._timeout(),
            verify=self._verify(),
        ) as client:
            try:
                response = await client.request(
                    method,
                    url,
                    json=payload,
                    params=params,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                duration_ms = round((time.perf_counter() - started_at) * 1000, 3)
                response_text = exc.response.text
                LOGGER.warning(
                    "mem0_upstream_request_failed",
                    extra=self._log_extra(
                        method=method,
                        path=path,
                        status_code=exc.response.status_code,
                        duration_ms=duration_ms,
                        error_type=type(exc).__name__,
                    ),
                )
                raise Mem0UpstreamError(
                    method=method,
                    path=path,
                    status_code=exc.response.status_code,
                    response_text=response_text,
                    outcome_unknown=False,
                    message=(
                        f"Mem0 upstream {method} {path} failed with "
                        f"HTTP {exc.response.status_code}: {response_text}"
                    ),
                ) from exc
            except httpx.RequestError as exc:
                duration_ms = round((time.perf_counter() - started_at) * 1000, 3)
                LOGGER.warning(
                    "mem0_upstream_request_failed",
                    extra=self._log_extra(
                        method=method,
                        path=path,
                        status_code=None,
                        duration_ms=duration_ms,
                        error_type=type(exc).__name__,
                    ),
                )
                raise Mem0UpstreamError(
                    method=method,
                    path=path,
                    outcome_unknown=True,
                    message=f"Mem0 upstream {method} {path} request failed: {exc}",
                ) from exc

            duration_ms = round((time.perf_counter() - started_at) * 1000, 3)
            decode_failed = False
            try:
                data = response.json()
            except Exception as exc:
                decode_failed = True
                LOGGER.warning(
                    "mem0_upstream_response_decode_failed",
                    extra=self._log_extra(
                        method=method,
                        path=path,
                        status_code=response.status_code,
                        duration_ms=duration_ms,
                        error_type=type(exc).__name__,
                    ),
                )
            if decode_failed:
                raise Mem0UpstreamError(
                    method=method,
                    path=path,
                    status_code=response.status_code,
                    response_text=None,
                    outcome_unknown=True,
                    message=(
                        f"Mem0 upstream {method} {path} returned an "
                        "undecodable success response"
                    ),
                )
            LOGGER.info(
                "mem0_upstream_request_completed",
                extra=self._log_extra(
                    method=method,
                    path=path,
                    status_code=response.status_code,
                    duration_ms=duration_ms,
                ),
            )
            if isinstance(data, dict):
                return data
            return {"results": data}

    def _log_extra(
        self,
        *,
        method: str,
        path: str,
        status_code: int | None,
        duration_ms: float,
        error_type: str | None = None,
    ) -> dict[str, Any]:
        return {
            "request_id": get_request_id(),
            "method": method,
            "path": path,
            "status_code": status_code,
            "duration_ms": duration_ms,
            "upstream_base_url": self.base_url,
            "error_type": error_type,
        }

    async def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_payload = dict(payload)
        if "messages" not in request_payload:
            content = request_payload.get("text")
            if content is None and "memory" in request_payload:
                content = request_payload["memory"]
            if content is not None:
                request_payload["messages"] = [
                    {"role": "user", "content": content},
                ]
        return await self._request("POST", self.memories_path, payload=request_payload)

    async def search_memories(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._request("POST", self.search_path, payload=payload)
        results = response.get("results")
        if not isinstance(results, list):
            return response
        normalized: list[Any] = []
        changed = False
        for item in results:
            if not isinstance(item, dict) or not isinstance(
                item.get("memory"), dict
            ):
                normalized.append(item)
                continue
            record = dict(item["memory"])
            if "score" in item:
                record["score"] = item["score"]
            normalized.append(record)
            changed = True
        if not changed:
            return response
        normalized_response = dict(response)
        normalized_response["results"] = normalized
        normalized_response.setdefault("total", len(normalized))
        return normalized_response

    async def list_memories(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._request("GET", self.memories_path, params=params)

    async def get_memory_history(self, memory_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            self._memory_item_path(memory_id, suffix="/history"),
        )

    async def get_memory(self, memory_id: str) -> dict[str, Any]:
        path = self._memory_item_path(memory_id)
        response = await self._request("GET", path)
        if response == {"results": None}:
            raise Mem0UpstreamError(
                method="GET",
                path=path,
                status_code=404,
                response_text=None,
                outcome_unknown=False,
                message=f"Mem0 upstream GET {path} returned no memory",
            )
        return response

    async def update_memory(
        self,
        memory_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request(
            "PUT",
            self._memory_item_path(memory_id),
            payload=payload,
        )

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        return await self._request("DELETE", self._memory_item_path(memory_id))

    async def delete_all_memories(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._request("DELETE", self.memories_path, params=params)
