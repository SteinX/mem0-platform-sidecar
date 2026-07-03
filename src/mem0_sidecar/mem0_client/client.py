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

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers(),
            transport=self.transport,
            timeout=30.0,
        ) as client:
            response = await client.post(path, json=payload)
            response.raise_for_status()
            return dict(response.json())

    async def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/v1/memories/", payload)

    async def search_memories(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/v1/memories/search/", payload)
