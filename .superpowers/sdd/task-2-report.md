## Task 2 Report

### What you implemented

- Added `src/mem0_sidecar/core/memory_ops.py` with:
  - `extract_memory_id(...)` handling `id`, `memory_id`, and first-result shapes
  - `MemoryService.add_memory(...)` that:
    - normalizes `user_id`, `agent_id`, `app_id`, and `run_id`
    - preserves `app_id` project isolation by defaulting to `project_id`
    - calls the Mem0 client only
    - records a durable `memory.add` event
    - writes a memory index projection
    - projects the app entity
  - `MemoryService.search_memories(...)` that forwards normalized scope filters
  - `MemoryService.get_memory(...)` passthrough
  - `MemoryService.delete_memory(...)` that records a durable `memory.delete` event and marks the projection deleted
- Added `tests/core/test_memory_ops.py` covering memory-id extraction, add flow projection/event behavior, and delete tombstoning.

### Test commands and results

- Focused RED:
  - `python -m pytest tests/core/test_memory_ops.py -v`
  - Result: failed during collection with `ModuleNotFoundError: No module named 'mem0_sidecar.core.memory_ops'`
- Focused GREEN:
  - `python -m pytest tests/core/test_memory_ops.py -v`
  - Result: `3 passed in 0.25s`
- Full suite:
  - `python -m pytest`
  - Result: `23 passed, 1 warning in 1.26s`

### TDD Evidence

#### RED command + relevant failing output + why expected

- Command:
  - `python -m pytest tests/core/test_memory_ops.py -v`
- Relevant output:

```text
collecting ... collected 0 items / 1 error
E   ModuleNotFoundError: No module named 'mem0_sidecar.core.memory_ops'
```

- Why expected:
  - The task required creating a new `memory_ops` module, so the test-first run should fail before implementation exists.

#### GREEN command + passing output

- Command:
  - `python -m pytest tests/core/test_memory_ops.py -v`
- Passing output:

```text
tests/core/test_memory_ops.py::test_extract_memory_id_accepts_common_shapes PASSED
tests/core/test_memory_ops.py::test_memory_service_adds_memory_indexes_projection_and_event PASSED
tests/core/test_memory_ops.py::test_memory_service_delete_marks_projection_deleted PASSED
============================== 3 passed in 0.25s ===============================
```

### Files changed

- `src/mem0_sidecar/core/memory_ops.py`
- `tests/core/test_memory_ops.py`
- `.superpowers/sdd/task-2-report.md`

### Self-review findings

- Kept implementation within the task-owned production file.
- Used existing repositories and helpers instead of adding new persistence paths.
- Confirmed mutating operations persist durable events and keep `app_id` set to the project when absent.
- Confirmed the service talks to the Mem0 client only and does not patch OSS internals or write to the OSS DB.

### Concerns, if any

- Full suite passes, but it still reports one pre-existing deprecation warning from `fastapi.testclient`/`starlette` using legacy `httpx` integration.
