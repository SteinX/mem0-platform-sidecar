## Task 2 Report

### What you implemented

- Updated `src/mem0_sidecar/core/memory_ops.py` so `MemoryService.get_memory(...)` now requires `project_id` plus `memory_id`, checks the local memory index first, and raises before any remote Mem0 read when the projection is missing or tombstoned.
- Updated `src/mem0_sidecar/store/repositories.py` so `MemoryIndexRepository.get_memory(...)` ignores soft-deleted rows by default, with an optional `include_deleted=True` path for admin/debug lookups.
- Expanded core tests to cover project-scoped reads, wrong-project rejection without a remote Mem0 call, and tombstoned delete behavior that does not re-hit Mem0.
- Expanded repository tests to cover the default non-deleted lookup path and the explicit `include_deleted=True` path.

### Test commands and results

- Focused RED:
  - `python -m pytest tests/core/test_memory_ops.py -v`
  - `python -m pytest tests/store/test_repositories.py -v`
  - Result: the new project-scoped `get_memory(...)` tests failed because the service still accepted only `memory_id`, and repository lookups still returned tombstoned rows.
- Focused GREEN:
  - `python -m pytest tests/core/test_memory_ops.py -v`
  - `python -m pytest tests/store/test_repositories.py -v`
  - Result: `8 passed` and `6 passed`
- Full suite:
  - `python -m pytest -v`
  - Result: `32 passed, 1 warning in 3.25s`

### TDD Evidence

#### RED command + relevant failing output + why expected

- Command:
  - `python -m pytest tests/core/test_memory_ops.py -v`
- Relevant output:

```text
TypeError: MemoryService.get_memory() got an unexpected keyword argument 'project_id'
```

- Why expected:
  - The service had not yet been changed to require project scope before reading from Mem0 OSS.

#### GREEN command + passing output

- Command:
  - `python -m pytest tests/core/test_memory_ops.py -v`
  - `python -m pytest tests/store/test_repositories.py -v`
- Passing output:

```text
tests/core/test_memory_ops.py::test_memory_service_get_memory_scopes_by_project_projection PASSED
tests/core/test_memory_ops.py::test_memory_service_get_memory_rejects_wrong_project_without_remote_call PASSED
tests/core/test_memory_ops.py::test_memory_service_delete_rejects_tombstoned_projection_without_remote_delete PASSED
tests/store/test_repositories.py::test_memory_index_repository_get_memory_ignores_deleted_by_default PASSED
tests/store/test_repositories.py::test_memory_index_repository_get_memory_can_include_deleted_rows PASSED
```

### Files changed

- `src/mem0_sidecar/core/memory_ops.py`
- `src/mem0_sidecar/store/repositories.py`
- `tests/core/test_memory_ops.py`
- `tests/store/test_repositories.py`
- `.superpowers/sdd/task-2-report.md`

### Self-review findings

- Core reads now verify local ownership before any remote Mem0 access.
- Tombstoned memory index rows are hidden by default, which prevents repeated delete/read paths from treating soft-deleted projections as live.
- The repository boundary stays intact; the service uses `MemoryIndexRepository` rather than direct ORM lookups.

### Concerns, if any

- None at the moment.
