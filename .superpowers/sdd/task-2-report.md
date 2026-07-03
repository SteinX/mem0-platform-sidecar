## Task 2 Report

### What you implemented

- Updated `src/mem0_sidecar/store/repositories.py` with `MemoryIndexRepository.get_memory(...)` so the core service can prove local ownership before delete.
- Updated `src/mem0_sidecar/core/memory_ops.py` so `MemoryService.delete_memory(...)` now:
  - loads the local projection first,
  - builds the durable `memory.delete` request from the projection scope when it exists,
  - creates a failed durable event and raises before any remote delete when the projection is missing for the project,
  - calls the remote Mem0 delete only after ownership is established,
  - then marks the local projection deleted.
- Expanded core and repository tests to cover the projection lookup, scope-preserving delete request, and wrong-project rejection without remote delete.

### Test commands and results

- Focused RED:
  - `python -m pytest tests/core/test_memory_ops.py -v`
  - `python -m pytest tests/store/test_repositories.py -v`
  - Result: the new delete-scope assertions failed and `MemoryIndexRepository.get_memory(...)` was missing.
- Focused GREEN:
  - `python -m pytest tests/core/test_memory_ops.py -v`
  - `python -m pytest tests/store/test_repositories.py -v`
  - Result: `6 passed` and `4 passed`
- Full suite:
  - `python -m pytest -v`
  - Result: `28 passed, 1 warning in 2.89s`

### TDD Evidence

#### RED command + relevant failing output + why expected

- Command:
  - `python -m pytest tests/core/test_memory_ops.py -v`
- Relevant output:

```text
FAILED tests/core/test_memory_ops.py::test_memory_service_delete_uses_projection_scope_for_event_request
E       AssertionError: assert {'memory_id': 'mem-1'} == {'agent_id': 'codex', ...}
FAILED tests/core/test_memory_ops.py::test_memory_service_delete_rejects_unknown_project_projection_without_remote_delete
E       Failed: DID NOT RAISE any of (<class 'KeyError'>, <class 'ValueError'>)
```

- Why expected:
  - Delete was still building the event request from only `memory_id`.
  - Delete still called the remote Mem0 client even when the memory belonged to a different project.

#### GREEN command + passing output

- Command:
  - `python -m pytest tests/core/test_memory_ops.py -v`
  - `python -m pytest tests/store/test_repositories.py -v`
- Passing output:

```text
tests/core/test_memory_ops.py::test_memory_service_delete_uses_projection_scope_for_event_request PASSED
tests/core/test_memory_ops.py::test_memory_service_delete_rejects_unknown_project_projection_without_remote_delete PASSED
tests/store/test_repositories.py::test_memory_index_repository_get_memory_scopes_by_project PASSED
```

### Files changed

- `src/mem0_sidecar/core/memory_ops.py`
- `src/mem0_sidecar/store/repositories.py`
- `tests/core/test_memory_ops.py`
- `tests/store/test_repositories.py`
- `.superpowers/sdd/task-2-report.md`

### Self-review findings

- The service now stays inside the repository boundary for ownership checks.
- The remote delete path is no longer reachable for a wrong-project memory id.
- The durable delete request carries the projection scope fields that are available locally.

### Concerns, if any

- One existing deprecation warning remains from `fastapi.testclient` / `starlette` using legacy `httpx` integration.
