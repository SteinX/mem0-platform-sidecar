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

## Task 4 Follow-up

This pass fixed two review findings:

- failed memory mutations now commit the `FAILED` event before the request exits
- event reads are scoped to the resolved project and no longer leak other projects' rows

## TDD Record

### RED

Added `tests/http_adapter/test_memory_routes.py` first, then ran:

```bash
python -m pytest tests/http_adapter/test_memory_routes.py -v
```

Observed expected failure:

- `test_failed_mutation_leaves_failed_event_queryable` failed because the event list was empty after a failed mutation
- `test_event_routes_do_not_leak_other_project_events` failed because `/v1/events` returned rows from both projects

### GREEN

Implemented the route changes and then ran:

```bash
python -m pytest tests/http_adapter/test_memory_routes.py tests/http_adapter/test_health.py -v
```

Result:

- `6 passed`

### Full Verification

Ran the full suite:

```bash
python -m pytest -v
```

Result:

- `37 passed`

## Files Changed

- `src/mem0_sidecar/http_adapter/memory_routes.py`
- `src/mem0_sidecar/http_adapter/event_routes.py`
- `src/mem0_sidecar/http_adapter/project_scope.py`
- `tests/http_adapter/test_memory_routes.py`

## Implementation Notes

### Memory routes

- Added a small shared `resolve_project_id(...)` helper that derives project scope from:
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

- The route layer now commits after both successful mutations and service failures.
- This preserves the `FAILED` event rows that `MemoryService` already created before re-raising, while still keeping the handler thin.

### Event routes

- Added list and detail event endpoints that resolve project scope through the same helper as memory routes.
- Both endpoints filter by `project_id`, so a raw event ID from another project now returns `404`.

## Behavior Verified

- Add route returns a Platform-shaped response containing both `memory` and `event`.
- Search route forwards normalized scope.
- Get route succeeds only after the memory projection exists in the scoped project.
- Delete route succeeds through the scoped projection path.
- Event list/detail routes expose stored operation history for the resolved project only.
- A failed memory mutation leaves a queryable `FAILED` event behind.

## Concerns

- Test output includes an existing `StarletteDeprecationWarning` from `fastapi.testclient` / `httpx`; this did not affect correctness and was not changed in this task.
