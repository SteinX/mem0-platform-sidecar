from typing import Any

import httpx


class Mem0RestClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.transport = transport

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        return {"Authorization": f"Bearer {self.api_key}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers(),
            transport=self.transport,
            timeout=30.0,
        ) as client:
            response = await client.request(
                method,
                path,
                json=payload,
                params=params,
            )
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                return data
            return {"results": data}

    async def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/v1/memories/", payload=payload)

    async def search_memories(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/v1/memories/search/", payload=payload)

    async def get_memory(self, memory_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/memories/{memory_id}/")

    async def update_memory(
        self,
        memory_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request("PUT", f"/v1/memories/{memory_id}/", payload=payload)

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/v1/memories/{memory_id}/")

    async def delete_all_memories(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._request("DELETE", "/v1/memories/", params=params)
