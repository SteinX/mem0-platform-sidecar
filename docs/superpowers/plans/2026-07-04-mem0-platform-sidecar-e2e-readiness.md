# Mem0 Platform Sidecar E2E Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the minimum Platform-compatible HTTP surface and live E2E harness needed to prove the sidecar can write, search, read, delete, index, and event memory operations against a real Mem0 OSS REST service.

**Architecture:** The sidecar exposes a narrow `/v3/memories/*`, `/v1/memories/{id}/`, and `/v1/events*` HTTP adapter backed by a new `MemoryService`. The service normalizes scope, calls Mem0 OSS through `Mem0RestClient`, persists sidecar projections and durable events in SQLite, and is tested first with mocked upstream HTTP and then with an opt-in live Mem0 OSS E2E test gated by environment variables.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2.x, httpx, pytest, SQLite, existing Phase 0/1 sidecar modules.

## Global Constraints

- Do not patch Mem0 OSS.
- Do not write directly to Mem0 OSS internal database tables.
- Treat Mem0 OSS REST as the only data-plane contract.
- Preserve `user_id`, `agent_id`, `app_id`, and `run_id` as first-class scope fields.
- Keep `app_id` project isolation intact; do not collapse unrelated workspaces into a default namespace.
- Store durable events for mutating sidecar operations even when the underlying operation is synchronous.
- Use SQLite first, with schema and repository boundaries that can support Postgres.
- Keep MCP handlers independent from HTTP route functions.
- Keep HTTP route functions independent from core service implementation details.
- Do not initialize git and do not create commits unless the user explicitly changes the no-commit instruction.

---

## Scope Check

This plan covers E2E readiness for sidecar HTTP-to-Mem0-OSS behavior:

- Mem0 REST client coverage for add, search, get, update, delete, and delete-all primitives.
- A `MemoryService` that wires Mem0 OSS calls to sidecar control-plane state.
- Minimal Platform-compatible HTTP routes for add/search/get/delete and event inspection.
- Mock-upstream route tests that exercise the full FastAPI app, DB, service, and Mem0 client boundary.
- Opt-in live E2E tests against a real Mem0 OSS REST base URL.
- E2E documentation and commands.

This plan does not cover:

- Hosted MCP tool implementation.
- Official Codex/OpenCode/OpenClaw/Hermes plugin smoke tests.
- Full `/v3/memories/` pagination parity.
- Update endpoint parity beyond client boundary support.
- Export/import jobs.
- Dashboard, analytics, webhooks, or admin UI.

## File Structure

Create or modify these files under `/workspace/data/mem0/mem0-platform-sidecar`:

```text
src/
  mem0_sidecar/
    core/
      memory_ops.py
    http_adapter/
      app.py
      dependencies.py
      event_routes.py
      memory_routes.py
    mem0_client/
      client.py
tests/
  e2e/
    test_live_mem0_oss.py
  http_adapter/
    test_memory_routes.py
  mem0_client/
    test_client.py
  core/
    test_memory_ops.py
docs/
  e2e.md
  development.md
```

## Task 1: Complete Mem0 REST Client Boundary

**Files:**
- Modify: `src/mem0_sidecar/mem0_client/client.py`
- Modify: `tests/mem0_client/test_client.py`

**Interfaces:**
- Consumes: existing `Mem0RestClient.add_memory(payload: dict[str, Any]) -> dict[str, Any]`
- Consumes: existing `Mem0RestClient.search_memories(payload: dict[str, Any]) -> dict[str, Any]`
- Produces: `Mem0RestClient.get_memory(memory_id: str) -> dict[str, Any]`
- Produces: `Mem0RestClient.update_memory(memory_id: str, payload: dict[str, Any]) -> dict[str, Any]`
- Produces: `Mem0RestClient.delete_memory(memory_id: str) -> dict[str, Any]`
- Produces: `Mem0RestClient.delete_all_memories(params: dict[str, Any]) -> dict[str, Any]`

- [ ] **Step 1: Write failing client tests**

Add these tests to `tests/mem0_client/test_client.py`:

```python
@pytest.mark.asyncio
async def test_mem0_client_posts_search_memory_payload() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/memories/search/"
        assert request.headers["authorization"] == "Bearer local-key"
        assert request.read() == b'{"query":"hello","user_id":"root"}'
        return httpx.Response(200, json={"results": [{"id": "mem-1"}]})

    client = Mem0RestClient(
        base_url="http://mem0.local",
        api_key="local-key",
        transport=httpx.MockTransport(handler),
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
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python -m pytest tests/mem0_client/test_client.py -v
```

Expected: FAIL because `get_memory` and `delete_memory` are not defined, and direct search coverage is newly asserted.

- [ ] **Step 3: Implement missing client methods**

Modify `src/mem0_sidecar/mem0_client/client.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify pass**

Run:

```bash
python -m pytest tests/mem0_client/test_client.py -v
```

Expected: PASS.

- [ ] **Step 5: No-commit checkpoint**

Append one line to `.superpowers/sdd/progress.md`:

```text
E2E Task 1: complete (no-commit, Mem0RestClient CRUD/search boundary covered)
```

## Task 2: Add Memory Service For Sidecar-To-OSS Operations

**Files:**
- Create: `src/mem0_sidecar/core/memory_ops.py`
- Create: `tests/core/test_memory_ops.py`

**Interfaces:**
- Consumes: `Mem0RestClient`
- Consumes: `CategoryRepository`
- Consumes: `EntityRepository`
- Consumes: `EventRepository`
- Consumes: `MemoryIndexRepository`
- Consumes: `normalize_scope(...)`
- Consumes: `extract_category(...)`
- Produces: `extract_memory_id(response: dict[str, Any]) -> str`
- Produces: `MemoryService.add_memory(project_id: str, payload: dict[str, Any]) -> dict[str, Any]`
- Produces: `MemoryService.search_memories(project_id: str, payload: dict[str, Any]) -> dict[str, Any]`
- Produces: `MemoryService.get_memory(memory_id: str) -> dict[str, Any]`
- Produces: `MemoryService.delete_memory(project_id: str, memory_id: str) -> dict[str, Any]`

- [ ] **Step 1: Write failing service tests**

Create `tests/core/test_memory_ops.py`:

```python
from typing import Any

import pytest

from mem0_sidecar.core.memory_ops import MemoryService, extract_memory_id
from mem0_sidecar.store.models import EventStatus, MemoryIndex
from mem0_sidecar.store.repositories import CategoryRepository, ProjectRepository


class FakeMem0Client:
    def __init__(self) -> None:
        self.add_payloads: list[dict[str, Any]] = []
        self.search_payloads: list[dict[str, Any]] = []
        self.deleted_ids: list[str] = []

    async def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.add_payloads.append(payload)
        return {"id": "mem-1", "memory": payload["text"]}

    async def search_memories(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.search_payloads.append(payload)
        return {"results": [{"id": "mem-1", "memory": "hello"}]}

    async def get_memory(self, memory_id: str) -> dict[str, Any]:
        return {"id": memory_id, "memory": "hello"}

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        self.deleted_ids.append(memory_id)
        return {"message": "Deleted"}


def test_extract_memory_id_accepts_common_shapes() -> None:
    assert extract_memory_id({"id": "mem-1"}) == "mem-1"
    assert extract_memory_id({"memory_id": "mem-2"}) == "mem-2"
    assert extract_memory_id({"results": [{"id": "mem-3"}]}) == "mem-3"


@pytest.mark.asyncio
async def test_memory_service_adds_memory_indexes_projection_and_event(db_session) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
        default_user_id="root",
        default_agent_id="codex",
    )
    CategoryRepository(db_session).replace_project_categories(
        project_id="repo-a",
        categories=[{"name": "decision", "description": "Architecture decisions"}],
    )
    mem0 = FakeMem0Client()
    service = MemoryService(session=db_session, mem0=mem0)

    result = await service.add_memory(
        project_id="repo-a",
        payload={
            "text": "Use a sidecar control plane",
            "user_id": "root",
            "agent_id": "codex",
            "metadata": {"type": "decision"},
        },
    )
    db_session.commit()

    indexed = db_session.query(MemoryIndex).filter_by(
        project_id="repo-a",
        mem0_memory_id="mem-1",
    ).one()
    assert result["memory"]["id"] == "mem-1"
    assert result["event"]["status"] == EventStatus.SUCCEEDED
    assert indexed.app_id == "repo-a"
    assert indexed.category == "decision"
    assert mem0.add_payloads[0]["user_id"] == "root"
    assert mem0.add_payloads[0]["app_id"] == "repo-a"


@pytest.mark.asyncio
async def test_memory_service_delete_marks_projection_deleted(db_session) -> None:
    ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    service = MemoryService(session=db_session, mem0=FakeMem0Client())
    await service.add_memory(project_id="repo-a", payload={"text": "delete me"})
    result = await service.delete_memory(project_id="repo-a", memory_id="mem-1")
    db_session.commit()

    indexed = db_session.query(MemoryIndex).filter_by(
        project_id="repo-a",
        mem0_memory_id="mem-1",
    ).one()
    assert result["memory"]["message"] == "Deleted"
    assert indexed.deleted_at is not None
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python -m pytest tests/core/test_memory_ops.py -v
```

Expected: FAIL with missing `mem0_sidecar.core.memory_ops`.

- [ ] **Step 3: Implement memory service**

Create `src/mem0_sidecar/core/memory_ops.py`:

```python
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from mem0_sidecar.core.categories import extract_category
from mem0_sidecar.core.scope import normalize_scope
from mem0_sidecar.store.models import EventStatus, MemoryIndex
from mem0_sidecar.store.repositories import (
    CategoryRepository,
    EntityRepository,
    EventRepository,
    MemoryIndexRepository,
)


def extract_memory_id(response: dict[str, Any]) -> str:
    if isinstance(response.get("id"), str):
        return response["id"]
    if isinstance(response.get("memory_id"), str):
        return response["memory_id"]
    results = response.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        first_id = results[0].get("id") or results[0].get("memory_id")
        if isinstance(first_id, str):
            return first_id
    raise ValueError(f"Could not extract memory id from response: {response!r}")


def _event_payload(event) -> dict[str, Any]:
    return {
        "id": event.id,
        "operation": event.operation,
        "status": event.status,
        "subject_type": event.subject_type,
        "subject_id": event.subject_id,
    }


class MemoryService:
    def __init__(self, *, session: Session, mem0) -> None:
        self.session = session
        self.mem0 = mem0

    async def add_memory(
        self,
        *,
        project_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        scope = normalize_scope(
            project_id=project_id,
            user_id=payload.get("user_id"),
            app_id=payload.get("app_id"),
            agent_id=payload.get("agent_id"),
            run_id=payload.get("run_id"),
        )
        metadata = dict(payload.get("metadata") or {})
        category_names = {
            category.name
            for category in CategoryRepository(self.session).list_project_categories(project_id)
        }
        category = extract_category(metadata, category_names)
        oss_payload = dict(payload)
        oss_payload.update(scope.as_filter_dict())
        event_repo = EventRepository(self.session)
        event = event_repo.create_event(
            project_id=project_id,
            operation="memory.add",
            request=payload,
            subject_type="memory",
        )
        try:
            memory_response = await self.mem0.add_memory(oss_payload)
            memory_id = extract_memory_id(memory_response)
            MemoryIndexRepository(self.session).upsert_memory(
                project_id=project_id,
                mem0_memory_id=memory_id,
                user_id=scope.user_id,
                app_id=scope.app_id,
                agent_id=scope.agent_id,
                run_id=scope.run_id,
                category=category,
                metadata=metadata,
            )
            EntityRepository(self.session).upsert_entity(
                project_id=project_id,
                entity_type="app",
                entity_id=scope.app_id,
                display_name=scope.app_id,
            )
            event.subject_id = memory_id
            event_repo.mark_succeeded(event.id, response=memory_response)
            return {"memory": memory_response, "event": _event_payload(event)}
        except Exception as exc:
            event_repo.mark_failed(event.id, error={"message": str(exc)})
            raise

    async def search_memories(
        self,
        *,
        project_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        scope = normalize_scope(
            project_id=project_id,
            user_id=payload.get("user_id"),
            app_id=payload.get("app_id"),
            agent_id=payload.get("agent_id"),
            run_id=payload.get("run_id"),
        )
        oss_payload = dict(payload)
        oss_payload.update(scope.as_filter_dict())
        return await self.mem0.search_memories(oss_payload)

    async def get_memory(self, *, memory_id: str) -> dict[str, Any]:
        return await self.mem0.get_memory(memory_id)

    async def delete_memory(
        self,
        *,
        project_id: str,
        memory_id: str,
    ) -> dict[str, Any]:
        event_repo = EventRepository(self.session)
        event = event_repo.create_event(
            project_id=project_id,
            operation="memory.delete",
            request={"memory_id": memory_id},
            subject_type="memory",
            subject_id=memory_id,
        )
        try:
            response = await self.mem0.delete_memory(memory_id)
            indexed = self.session.query(MemoryIndex).filter_by(
                project_id=project_id,
                mem0_memory_id=memory_id,
            ).one_or_none()
            if indexed is not None:
                indexed.deleted_at = datetime.now(UTC)
            event_repo.mark_succeeded(event.id, response=response)
            return {"memory": response, "event": _event_payload(event)}
        except Exception as exc:
            event_repo.mark_failed(event.id, error={"message": str(exc)})
            raise
```

- [ ] **Step 4: Run tests to verify pass**

Run:

```bash
python -m pytest tests/core/test_memory_ops.py -v
```

Expected: PASS.

- [ ] **Step 5: No-commit checkpoint**

Append:

```text
E2E Task 2: complete (no-commit, MemoryService add/search/get/delete core added)
```

## Task 3: Wire App Dependencies For Database And Mem0 Client Injection

**Files:**
- Create: `src/mem0_sidecar/http_adapter/dependencies.py`
- Modify: `src/mem0_sidecar/http_adapter/app.py`
- Modify: `tests/http_adapter/test_health.py`

**Interfaces:**
- Consumes: `create_engine_from_url(database_url: str)`
- Consumes: `create_session_factory(engine: Engine)`
- Consumes: `Base.metadata.create_all(engine)`
- Consumes: `Mem0RestClient`
- Consumes: `ProjectRepository.upsert_default_project(...)`
- Produces: `create_app(settings: SidecarSettings | None = None, session_factory=None, mem0_client=None) -> FastAPI`
- Produces: `get_session(request: Request) -> Iterator[Session]`
- Produces: `get_mem0_client(request: Request) -> Mem0RestClient`

- [ ] **Step 1: Write failing dependency-aware app test**

Modify `tests/http_adapter/test_health.py`:

```python
from fastapi.testclient import TestClient

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app


def test_healthz_reports_sidecar_status() -> None:
    client = TestClient(create_app())

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "mem0-platform-sidecar"}


def test_create_app_stores_injected_settings_and_clients(tmp_path) -> None:
    settings = SidecarSettings(
        database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
        mem0_base_url="http://mem0.local",
    )
    mem0_client = object()
    app = create_app(settings=settings, mem0_client=mem0_client)

    assert app.state.settings is settings
    assert app.state.mem0_client is mem0_client
    assert app.state.session_factory is not None
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python -m pytest tests/http_adapter/test_health.py -v
```

Expected: FAIL because `create_app` does not accept `mem0_client` or set `session_factory`.

- [ ] **Step 3: Implement dependency helpers**

Create `src/mem0_sidecar/http_adapter/dependencies.py`:

```python
from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session


def get_session(request: Request) -> Iterator[Session]:
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        yield session


def get_mem0_client(request: Request):
    return request.app.state.mem0_client
```

Modify `src/mem0_sidecar/http_adapter/app.py`:

```python
from fastapi import FastAPI

from mem0_sidecar.config import SidecarSettings, load_settings
from mem0_sidecar.mem0_client.client import Mem0RestClient
from mem0_sidecar.store.database import create_engine_from_url, create_session_factory
from mem0_sidecar.store.models import Base
from mem0_sidecar.store.repositories import ProjectRepository


def create_app(
    settings: SidecarSettings | None = None,
    *,
    session_factory=None,
    mem0_client=None,
) -> FastAPI:
    settings = settings or load_settings()

    if session_factory is None:
        engine = create_engine_from_url(settings.database_url)
        Base.metadata.create_all(engine)
        session_factory = create_session_factory(engine)
    with session_factory() as session:
        ProjectRepository(session).upsert_default_project(
            project_id=settings.default_project_id,
            name=settings.default_project_id,
            mem0_base_url=settings.mem0_base_url,
        )
        session.commit()
    if mem0_client is None:
        mem0_client = Mem0RestClient(
            base_url=settings.mem0_base_url,
            api_key=settings.mem0_api_key,
        )

    app = FastAPI(title="Mem0 Platform Sidecar")
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.mem0_client = mem0_client

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "mem0-platform-sidecar"}

    return app
```

- [ ] **Step 4: Run tests to verify pass**

Run:

```bash
python -m pytest tests/http_adapter/test_health.py -v
```

Expected: PASS.

- [ ] **Step 5: No-commit checkpoint**

Append:

```text
E2E Task 3: complete (no-commit, app dependency injection added)
```

## Task 4: Add Minimal Platform-Compatible Memory And Event Routes

**Files:**
- Create: `src/mem0_sidecar/http_adapter/memory_routes.py`
- Create: `src/mem0_sidecar/http_adapter/event_routes.py`
- Modify: `src/mem0_sidecar/http_adapter/app.py`
- Create: `tests/http_adapter/test_memory_routes.py`

**Interfaces:**
- Consumes: `MemoryService`
- Consumes: `get_session`
- Consumes: `get_mem0_client`
- Consumes: `EventRepository`
- Produces: `memory_router`
- Produces: `event_router`
- Produces: HTTP `POST /v3/memories/add/`
- Produces: HTTP `POST /v3/memories/search/`
- Produces: HTTP `GET /v1/memories/{memory_id}/`
- Produces: HTTP `DELETE /v1/memories/{memory_id}/`
- Produces: HTTP `GET /v1/events`
- Produces: HTTP `GET /v1/event/{event_id}`

- [ ] **Step 1: Write failing route tests with fake Mem0 client**

Create `tests/http_adapter/test_memory_routes.py`:

```python
from typing import Any

from fastapi.testclient import TestClient

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app


class FakeMem0Client:
    def __init__(self) -> None:
        self.add_payloads: list[dict[str, Any]] = []
        self.search_payloads: list[dict[str, Any]] = []

    async def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.add_payloads.append(payload)
        return {"id": "mem-1", "memory": payload["text"]}

    async def search_memories(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.search_payloads.append(payload)
        return {"results": [{"id": "mem-1", "memory": "hello"}]}

    async def get_memory(self, memory_id: str) -> dict[str, Any]:
        return {"id": memory_id, "memory": "hello"}

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
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

    delete_response = client.delete("/v1/memories/mem-1/")
    assert delete_response.status_code == 200
    assert delete_response.json()["memory"]["message"] == "Deleted mem-1"

    events_response = client.get("/v1/events")
    assert events_response.status_code == 200
    assert len(events_response.json()["results"]) >= 2
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python -m pytest tests/http_adapter/test_memory_routes.py -v
```

Expected: FAIL with 404 or missing route modules.

- [ ] **Step 3: Implement routes**

Create `src/mem0_sidecar/http_adapter/memory_routes.py`:

```python
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from mem0_sidecar.core.memory_ops import MemoryService
from mem0_sidecar.http_adapter.dependencies import get_mem0_client, get_session

memory_router = APIRouter()


def _project_id(request: Request, payload: dict[str, Any] | None = None) -> str:
    if payload and isinstance(payload.get("project_id"), str):
        return payload["project_id"]
    if payload and isinstance(payload.get("app_id"), str):
        return payload["app_id"]
    return request.app.state.settings.default_project_id


@memory_router.post("/v3/memories/add/")
@memory_router.post("/v3/memories/add", include_in_schema=False)
async def add_memory(
    payload: dict[str, Any],
    request: Request,
    session: Session = Depends(get_session),
    mem0=Depends(get_mem0_client),
) -> dict[str, Any]:
    return await MemoryService(session=session, mem0=mem0).add_memory(
        project_id=_project_id(request, payload),
        payload=payload,
    )


@memory_router.post("/v3/memories/search/")
@memory_router.post("/v3/memories/search", include_in_schema=False)
async def search_memories(
    payload: dict[str, Any],
    request: Request,
    session: Session = Depends(get_session),
    mem0=Depends(get_mem0_client),
) -> dict[str, Any]:
    return await MemoryService(session=session, mem0=mem0).search_memories(
        project_id=_project_id(request, payload),
        payload=payload,
    )


@memory_router.get("/v1/memories/{memory_id}/")
@memory_router.get("/v1/memories/{memory_id}", include_in_schema=False)
async def get_memory(
    memory_id: str,
    session: Session = Depends(get_session),
    mem0=Depends(get_mem0_client),
) -> dict[str, Any]:
    return await MemoryService(session=session, mem0=mem0).get_memory(
        memory_id=memory_id,
    )


@memory_router.delete("/v1/memories/{memory_id}/")
@memory_router.delete("/v1/memories/{memory_id}", include_in_schema=False)
async def delete_memory(
    memory_id: str,
    request: Request,
    session: Session = Depends(get_session),
    mem0=Depends(get_mem0_client),
) -> dict[str, Any]:
    return await MemoryService(session=session, mem0=mem0).delete_memory(
        project_id=request.app.state.settings.default_project_id,
        memory_id=memory_id,
    )
```

Create `src/mem0_sidecar/http_adapter/event_routes.py`:

```python
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from mem0_sidecar.http_adapter.dependencies import get_session
from mem0_sidecar.store.models import Event
from mem0_sidecar.store.repositories import EventRepository

event_router = APIRouter()


def _event_to_dict(event: Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "project_id": event.project_id,
        "operation": event.operation,
        "status": event.status,
        "subject_type": event.subject_type,
        "subject_id": event.subject_id,
        "request": json.loads(event.request_json),
        "response": json.loads(event.response_json),
        "error": json.loads(event.error_json),
    }


@event_router.get("/v1/events")
@event_router.get("/v1/events/", include_in_schema=False)
def list_events(session: Session = Depends(get_session)) -> dict[str, Any]:
    events = session.scalars(select(Event).order_by(Event.created_at)).all()
    return {"results": [_event_to_dict(event) for event in events]}


@event_router.get("/v1/event/{event_id}")
@event_router.get("/v1/event/{event_id}/", include_in_schema=False)
def get_event_status(
    event_id: str,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    try:
        return _event_to_dict(EventRepository(session).get(event_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Event not found") from exc
```

Modify `src/mem0_sidecar/http_adapter/app.py` to include routers:

```python
from fastapi import FastAPI

from mem0_sidecar.config import SidecarSettings, load_settings
from mem0_sidecar.http_adapter.event_routes import event_router
from mem0_sidecar.http_adapter.memory_routes import memory_router
from mem0_sidecar.mem0_client.client import Mem0RestClient
from mem0_sidecar.store.database import create_engine_from_url, create_session_factory
from mem0_sidecar.store.models import Base
from mem0_sidecar.store.repositories import ProjectRepository


def create_app(
    settings: SidecarSettings | None = None,
    *,
    session_factory=None,
    mem0_client=None,
) -> FastAPI:
    settings = settings or load_settings()

    if session_factory is None:
        engine = create_engine_from_url(settings.database_url)
        Base.metadata.create_all(engine)
        session_factory = create_session_factory(engine)
    with session_factory() as session:
        ProjectRepository(session).upsert_default_project(
            project_id=settings.default_project_id,
            name=settings.default_project_id,
            mem0_base_url=settings.mem0_base_url,
        )
        session.commit()
    if mem0_client is None:
        mem0_client = Mem0RestClient(
            base_url=settings.mem0_base_url,
            api_key=settings.mem0_api_key,
        )

    app = FastAPI(title="Mem0 Platform Sidecar")
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.mem0_client = mem0_client
    app.include_router(memory_router)
    app.include_router(event_router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "mem0-platform-sidecar"}

    return app
```

- [ ] **Step 4: Run tests to verify pass**

Run:

```bash
python -m pytest tests/http_adapter/test_memory_routes.py tests/http_adapter/test_health.py -v
```

Expected: PASS.

- [ ] **Step 5: No-commit checkpoint**

Append:

```text
E2E Task 4: complete (no-commit, minimal Platform HTTP routes wired)
```

## Task 5: Add Live Mem0 OSS E2E Test

**Files:**
- Create: `tests/e2e/test_live_mem0_oss.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `create_app(settings=...)`
- Consumes: env `MEM0_E2E_BASE_URL`
- Consumes: optional env `MEM0_E2E_API_KEY`
- Consumes: optional env `MEM0_E2E_PROJECT_ID`
- Produces: pytest marker `e2e`

- [ ] **Step 1: Add pytest marker**

Modify `[tool.pytest.ini_options]` in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
asyncio_mode = "auto"
markers = [
  "e2e: tests that require a live Mem0 OSS-compatible REST service",
]
```

- [ ] **Step 2: Write skipped-by-default live E2E test**

Create `tests/e2e/test_live_mem0_oss.py`:

```python
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app


pytestmark = pytest.mark.e2e


def _live_settings(tmp_path) -> SidecarSettings:
    base_url = os.environ.get("MEM0_E2E_BASE_URL")
    if not base_url:
        pytest.skip("MEM0_E2E_BASE_URL is not set")
    return SidecarSettings(
        database_url=f"sqlite:///{tmp_path / 'sidecar-e2e.sqlite3'}",
        mem0_base_url=base_url,
        mem0_api_key=os.environ.get("MEM0_E2E_API_KEY"),
        default_project_id=os.environ.get("MEM0_E2E_PROJECT_ID", "sidecar-e2e"),
    )


def test_live_sidecar_add_search_get_delete_against_mem0_oss(tmp_path) -> None:
    settings = _live_settings(tmp_path)
    client = TestClient(create_app(settings=settings))
    marker = f"sidecar-e2e-{uuid4()}"

    add_response = client.post(
        "/v3/memories/add/",
        json={
            "text": f"Remember {marker}",
            "user_id": "sidecar-e2e-user",
            "app_id": settings.default_project_id,
            "metadata": {"type": "e2e", "marker": marker},
        },
    )
    assert add_response.status_code == 200, add_response.text
    add_body = add_response.json()
    memory_id = add_body["memory"].get("id") or add_body["memory"].get("memory_id")
    assert memory_id
    assert add_body["event"]["status"] == "SUCCEEDED"

    search_response = client.post(
        "/v3/memories/search/",
        json={
            "query": marker,
            "user_id": "sidecar-e2e-user",
            "app_id": settings.default_project_id,
        },
    )
    assert search_response.status_code == 200, search_response.text
    search_body = search_response.json()
    assert memory_id in str(search_body) or marker in str(search_body)

    get_response = client.get(f"/v1/memories/{memory_id}/")
    assert get_response.status_code == 200, get_response.text
    assert get_response.json().get("id") == memory_id

    delete_response = client.delete(f"/v1/memories/{memory_id}/")
    assert delete_response.status_code == 200, delete_response.text
    assert delete_response.json()["event"]["status"] == "SUCCEEDED"

    events_response = client.get("/v1/events")
    assert events_response.status_code == 200
    operations = [event["operation"] for event in events_response.json()["results"]]
    assert "memory.add" in operations
    assert "memory.delete" in operations
```

- [ ] **Step 3: Run without env to verify skip**

Run:

```bash
unset MEM0_E2E_BASE_URL
python -m pytest tests/e2e/test_live_mem0_oss.py -v
```

Expected: SKIPPED with message `MEM0_E2E_BASE_URL is not set`.

- [ ] **Step 4: Run with live Mem0 OSS**

Run when a live Mem0 OSS REST service is available:

```bash
MEM0_E2E_BASE_URL=http://127.0.0.1:8000 \
MEM0_E2E_PROJECT_ID=sidecar-e2e \
python -m pytest tests/e2e/test_live_mem0_oss.py -v
```

Expected: PASS and the memory created by the marker is deleted before test exit.

- [ ] **Step 5: No-commit checkpoint**

Append:

```text
E2E Task 5: complete (no-commit, live Mem0 OSS E2E test added)
```

## Task 6: Document E2E Workflow

**Files:**
- Create: `docs/e2e.md`
- Modify: `docs/development.md`

**Interfaces:**
- Consumes: env `MEM0_E2E_BASE_URL`
- Consumes: env `MEM0_E2E_API_KEY`
- Consumes: env `MEM0_E2E_PROJECT_ID`
- Produces: documented commands for mock, skipped, and live E2E runs

- [ ] **Step 1: Write E2E docs**

Create `docs/e2e.md`:

````markdown
# E2E Testing

The sidecar has two E2E levels.

## Mock-Upstream E2E

This runs the FastAPI app, SQLite sidecar database, `MemoryService`, and Mem0
client boundary against a fake upstream client.

```bash
python -m pytest tests/http_adapter/test_memory_routes.py -v
```

## Live Mem0 OSS E2E

This runs the FastAPI sidecar app in-process while the sidecar calls a real
Mem0 OSS-compatible REST service over HTTP.

Required:

- `MEM0_E2E_BASE_URL`

Optional:

- `MEM0_E2E_API_KEY`
- `MEM0_E2E_PROJECT_ID`

Example:

```bash
MEM0_E2E_BASE_URL=http://127.0.0.1:8000 \
MEM0_E2E_PROJECT_ID=sidecar-e2e \
python -m pytest tests/e2e/test_live_mem0_oss.py -v
```

When `MEM0_E2E_BASE_URL` is not set, live E2E tests skip instead of failing.
The live test creates a marker memory, searches it, reads it, deletes it, and
asserts durable sidecar add/delete events.
````

- [ ] **Step 2: Link from development docs**

Append to `docs/development.md`:

```markdown

## E2E

See [E2E Testing](e2e.md).
```

- [ ] **Step 3: Verify docs commands that do not require live Mem0 OSS**

Run:

```bash
python -m pytest tests/http_adapter/test_memory_routes.py -v
unset MEM0_E2E_BASE_URL
python -m pytest tests/e2e/test_live_mem0_oss.py -v
```

Expected: first command PASS; second command SKIPPED.

- [ ] **Step 4: No-commit checkpoint**

Append:

```text
E2E Task 6: complete (no-commit, E2E workflow documented)
```

## Final Verification

- [ ] **Step 1: Run unit and integration tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
```

Expected: PASS with live E2E skipped when `MEM0_E2E_BASE_URL` is absent.

- [ ] **Step 2: Run lint**

Run:

```bash
python -m ruff check . --no-cache
```

Expected: PASS.

- [ ] **Step 3: Run live E2E when Mem0 OSS is available**

Run:

```bash
MEM0_E2E_BASE_URL=http://127.0.0.1:8000 \
MEM0_E2E_PROJECT_ID=sidecar-e2e \
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/e2e/test_live_mem0_oss.py -q -p no:cacheprovider
```

Expected: PASS. If no local Mem0 OSS service is available, record this as not run rather than silently claiming E2E completion.

- [ ] **Step 4: Confirm no generated artifacts remain**

Run:

```bash
find /workspace/data/mem0/mem0-platform-sidecar -maxdepth 5 -type f \
  \( -path '*/__pycache__/*' -o -path '*/.pytest_cache/*' -o \
     -path '*/.ruff_cache/*' -o -path '*egg-info/*' -o \
     -name 'scratch.sqlite3' -o -name 'mem0_sidecar.sqlite3' \) -print
```

Expected: no output after cleanup.

- [ ] **Step 5: Confirm no git repo was initialized**

Run:

```bash
test -d /workspace/data/mem0/mem0-platform-sidecar/.git
```

Expected: exit code 1.
