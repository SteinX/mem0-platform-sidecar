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

## Re-review Follow-up

This pass addressed the scope/bootstrap issues raised in re-review:

- non-default projects are now materialized through route traffic with `ProjectRepository.upsert_default_project(...)`
- payload/query conflicts between `project_id` and `app_id` now return `400`
- add/search payloads normalize `app_id` to the resolved project before reaching `MemoryService`

### RED

Added new coverage in `tests/http_adapter/test_memory_routes.py` and ran:

```bash
python -m pytest tests/http_adapter/test_memory_routes.py -v
```

Observed expected failures before the fix:

- non-default project bootstrap assertions failed because the project row was never created by route traffic
- conflicting `project_id` / `app_id` requests were still accepted

### GREEN

After implementing the route helpers and project bootstrap, reran:

```bash
python -m pytest tests/http_adapter/test_memory_routes.py tests/http_adapter/test_health.py -v
python -m pytest -v
```

Results:

- `8 passed`
- `39 passed`

## Task 4 Payload Normalization Follow-up (project_id stripping)

### RED (requested verification step)

Ran baseline verification before introducing the strip behavior:

```bash
python -m pytest tests/http_adapter/test_memory_routes.py -v
```

Observed: `5 passed` (baseline did not include the `project_id` stripping assertion, so existing suite remained green).

### GREEN (post-fix)

Updated `test_route_scoped_requests_bootstrap_non_default_project_and_normalize_app_id` to send payload-level `project_id` for add/search and assert it is not forwarded:

- `mem0.add_payloads[0]` has no `project_id`, `app_id == resolved project`
- `mem0.search_payloads[0]` has no `project_id`, `app_id == resolved project`

Validation command:

```bash
python -m pytest tests/http_adapter/test_memory_routes.py tests/http_adapter/test_health.py -v
python -m pytest -v
```

Results:

- `8 passed`
- `39 passed`

## Task 4 Delete Scope Follow-up

The earlier note that conflicting `project_id` / `app_id` requests were rejected or returned `400` is stale. Current intended semantics allow differing values, preserve the explicit `app_id`, and use sidecar `project_id` for local project scope.

### Fix note

Added regression coverage for `DELETE /v1/memories/mem-1?project_id=repo-a&app_id=app-x` to verify:

- the response is `404`
- no remote delete call is made
- `repo-a` is bootstrapped with `default_app_id == app-x`
- the failed `memory.delete` event is recorded under `project_id == repo-a`
- `event.request_json` keeps `memory_id == mem-1` and `app_id == app-x`

Verification commands:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/http_adapter/test_memory_routes.py -q -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
```

## Task 4 Final Review Fixes

This pass addressed the final review findings around event read boundaries and failed-event durability.

### RED

Added coverage first in:

- `tests/store/test_repositories.py`
- `tests/core/test_events.py`
- `tests/core/test_memory_ops.py`
- `tests/http_adapter/test_memory_routes.py`

Red command:

```bash
python -m pytest tests/core/test_events.py tests/core/test_memory_ops.py tests/store/test_repositories.py tests/http_adapter/test_memory_routes.py -v
```

Observed expected failures before the fix:

- `EventRepository` had no project-scoped event list/get methods
- `EventService` had no project-scoped serialized list/get methods
- `GET /v1/events?project_id=repo-z` bootstrapped `repo-z` as a side effect

### GREEN

Implemented the fix by:

- adding `EventRepository.list_project_events(...)` and `get_project_event(...)`
- moving event JSON decoding into `EventService`
- making `MemoryService.add_memory()` and `delete_memory()` commit after `mark_failed(...)` before re-raising
- changing `memory_routes.py` failure handling to `rollback()` instead of broad `commit()` paths
- routing event read endpoints through `EventService` without auto-creating projects

Green command:

```bash
python -m pytest tests/core/test_events.py tests/core/test_memory_ops.py tests/store/test_repositories.py tests/http_adapter/test_memory_routes.py -v
```

Result:

- `25 passed`

### Required Verification

## Re-review Follow-up: Query App Scope Preservation

This pass fixed the final re-review findings around query-supplied `app_id` and project bootstrap defaults.

### RED

Added regression coverage first in `tests/http_adapter/test_memory_routes.py` and ran:

```bash
python -m pytest tests/http_adapter/test_memory_routes.py -v
```

Observed expected failures before the fix:

- `POST /v3/memories/add/?project_id=repo-a&app_id=app-x` forwarded `app_id=repo-a` instead of preserving `app-x`
- `POST /v3/memories/search/?project_id=repo-a&app_id=app-x` forwarded `app_id=repo-a` instead of preserving `app-x`

### GREEN

Implemented the route-scope fix by:

- preserving query-supplied `app_id` in the payload forwarded to `MemoryService`
- passing resolved `default_app_id` into `ensure_project(...)` for mutating routes
- leaving search read-only and without project bootstrap side effects

### Verification

Ran:

```bash
python -m pytest tests/http_adapter/test_memory_routes.py -v
python -m pytest tests/http_adapter/test_memory_routes.py tests/store/test_repositories.py -v
python -m pytest tests/http_adapter/test_memory_routes.py tests/http_adapter/test_health.py -v
python -m pytest -v
```

Results:

- `12 passed`
- `22 passed`
- `15 passed`
- `53 passed`

See the scope semantics fix below.

## Task 4 Scope Semantics Review Fix

This pass corrected the project/app scope split requested in the review:

- `project_id` now controls the local sidecar project scope
- `app_id` is preserved as the Mem0 data-plane scope when present
- search no longer writes project rows as a read-side effect
- `ProjectRepository.upsert_default_project(...)` preserves `default_app_id` unless explicitly changed

### RED

Added the requested coverage first in:

- `tests/http_adapter/test_memory_routes.py`
- `tests/store/test_repositories.py`

Red command:

```bash
python -m pytest tests/http_adapter/test_memory_routes.py tests/store/test_repositories.py -v
```

Observed expected failures before the fix:

- add/search requests with differing `project_id` and `app_id` were rejected
- `ProjectRepository.upsert_default_project(...)` did not accept `default_app_id`

### GREEN

Implemented the routing and repository changes, then reran:

```bash
python -m pytest tests/http_adapter/test_memory_routes.py tests/store/test_repositories.py -v
python -m pytest tests/http_adapter/test_memory_routes.py tests/http_adapter/test_health.py -v
python -m pytest -q
```

Results:

- `20 passed`
- `13 passed`
- `51 passed`

## Task 4 Latest Review Fixes

This pass addressed the last two review findings:

- `ProjectRepository.upsert_default_project(...)` now preserves existing `default_user_id` and `default_agent_id` when routed upserts omit them
- `GET /v1/memories/{memory_id}` no longer bootstraps a project row on read

### RED

Added the new regressions first in:

- `tests/store/test_repositories.py`
- `tests/http_adapter/test_memory_routes.py`

Observed expected failures before the fix:

- routed project upserts cleared persisted project defaults
- unknown-project memory reads created a `projects` row as a side effect

### GREEN

Implemented the fix and reran the requested checks:

```bash
python -m pytest tests/http_adapter/test_memory_routes.py tests/store/test_repositories.py -v
python -m pytest tests/http_adapter/test_memory_routes.py tests/http_adapter/test_health.py -v
python -m pytest
```

Results:

- `15 passed`
- `10 passed`
- `46 passed`

Commands run:

```bash
python -m pytest tests/http_adapter/test_memory_routes.py tests/http_adapter/test_health.py -v
python -m pytest tests/core/test_events.py tests/core/test_memory_ops.py tests/store/test_repositories.py -v
python -m pytest -v
```

Results:

- `9 passed`
- `19 passed`
- `44 passed`

## Files Changed In Final Review Pass

- `src/mem0_sidecar/core/events.py`
- `src/mem0_sidecar/core/memory_ops.py`
- `src/mem0_sidecar/store/repositories.py`
- `src/mem0_sidecar/http_adapter/memory_routes.py`
- `src/mem0_sidecar/http_adapter/event_routes.py`
- `tests/core/test_events.py`
- `tests/core/test_memory_ops.py`
- `tests/store/test_repositories.py`
- `tests/http_adapter/test_memory_routes.py`
