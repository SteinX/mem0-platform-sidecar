# Task 4 Report: Minimal Platform-Compatible Memory And Event Routes

## Outcome

Implemented minimal Platform-compatible HTTP routes for memories and events:

- `POST /v3/memories/add/`
- `POST /v3/memories/search/`
- `GET /v1/memories/{memory_id}/`
- `DELETE /v1/memories/{memory_id}/`
- `GET /v1/events`
- `GET /v1/event/{event_id}`

The routes are now wired into `create_app(...)` and operate through injected dependencies instead of reaching into core internals directly.

## TDD Record

### RED

Added `tests/http_adapter/test_memory_routes.py` first, then ran:

```bash
python -m pytest tests/http_adapter/test_memory_routes.py -v
```

Observed expected failure:

- `404 Not Found` on `POST /v3/memories/add/`

### GREEN

Implemented the route modules and app wiring, then ran:

```bash
python -m pytest tests/http_adapter/test_memory_routes.py tests/http_adapter/test_health.py -v
```

Result:

- `4 passed`

### Full Verification

Ran the full suite:

```bash
python -m pytest -v
```

Result:

- `35 passed`

## Files Changed

- `src/mem0_sidecar/http_adapter/memory_routes.py`
- `src/mem0_sidecar/http_adapter/event_routes.py`
- `src/mem0_sidecar/http_adapter/app.py`
- `tests/http_adapter/test_memory_routes.py`

## Implementation Notes

### Memory routes

- Added a small `_project_id(...)` resolver in the HTTP layer that derives project scope from:
  1. request payload `project_id`
  2. request payload `app_id`
  3. query param `project_id`
  4. query param `app_id`
  5. `settings.default_project_id`

- `add` and `search` pass the derived `project_id` into `MemoryService`.
- `get` and `delete` now honor Task 2 project isolation by calling:
  - `get_memory(project_id=..., memory_id=...)`
  - `delete_memory(project_id=..., memory_id=...)`

### Transaction boundary

- The route layer now commits successful mutating operations (`add`, `delete`) and rolls back on failure.
- This was necessary so the projection and event rows created by `MemoryService` are persisted across request-scoped sessions.

### Event routes

- Added list and detail event endpoints that serialize stored event JSON fields back into dictionaries.
- `GET /v1/event/{event_id}` returns `404` when the event does not exist.

## Behavior Verified

- Add route returns a Platform-shaped response containing both `memory` and `event`.
- Search route forwards normalized scope.
- Get route succeeds only after the memory projection exists in the scoped project.
- Delete route succeeds through the scoped projection path.
- Event list/detail routes expose stored operation history after memory mutations.

## Concerns

- Test output includes an existing `StarletteDeprecationWarning` from `fastapi.testclient` / `httpx`; this did not affect correctness and was not changed in this task.
