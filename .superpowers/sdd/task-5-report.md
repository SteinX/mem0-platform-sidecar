Task 5 report: Live E2E For Categories And Export

Summary
- Added `test_live_categories_and_export_flow` to `tests/e2e/test_live_mem0_oss.py`.
- Kept the existing sidecar `TestClient(create_app(settings=_live_settings(tmp_path)))` live E2E pattern.
- Covered category configuration, add, export creation, export download, payload verification, and cleanup against the local compose-backed Mem0 OSS stack.

TDD notes
- Added the new live E2E test first.
- Focused normal pytest run outside the compose harness produced the expected skip because `MEM0_E2E_BASE_URL` was unset.
- No production-code changes were required because Tasks 1 and 4 already supplied the routes and behavior this task needed to prove live.

Verification
- `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests/e2e/test_live_mem0_oss.py -k categories_and_export`
  - Result: skipped outside compose runner as expected.
- `PYTHONDONTWRITEBYTECODE=1 python scripts/run_live_e2e_compose.py`
  - Result: passed with exit code 0 against the local temporary Mem0 OSS/Postgres/OpenAI stub/e2e-runner stack and cleaned up with compose down.
  - Note: in this worktree, the compose file's existing `../../upstream` path assumption required a temporary local symlink outside the repo change set so the harness could resolve the upstream Mem0 build context.
- `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider`
  - Result: `101 passed, 2 skipped`.
- `python -m ruff check . --no-cache`
  - Result: passed.

Files changed
- `tests/e2e/test_live_mem0_oss.py`

Files intentionally not changed
- `scripts/run_live_e2e_compose.py`

Commit
- `test: cover dashboard categories export e2e`

---

Review follow-up fix (2026-07-05)

Summary
- Fixed the live compose harness so linked worktrees resolve the Mem0 upstream build context from checked-in state instead of relying on a manual symlink.
- Added harness tests for the default git-layout resolver and explicit `MEM0_E2E_UPSTREAM_CONTEXT` override.
- Strengthened the live category/export E2E to prove export filters exclude an out-of-scope memory from the same project and clean up both created memories.

TDD notes
- Added failing harness coverage first by importing a not-yet-implemented `resolve_upstream_context` helper in `tests/test_e2e_compose_harness.py`; focused pytest failed at collection with `ImportError: cannot import name 'resolve_upstream_context'`.
- Implemented the minimal resolver/env plumbing in `scripts/run_live_e2e_compose.py` and switched `docker/docker-compose.e2e.yml` to consume `MEM0_E2E_UPSTREAM_CONTEXT`.
- Updated the live E2E export test to create one in-scope memory and one out-of-scope memory with different `app_id` and `user_id`, then asserted the downloaded export includes only the in-scope marker.

Verification
- `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests/test_e2e_compose_harness.py`
  - Result: `6 passed in 0.05s`.
- `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests/e2e/test_live_mem0_oss.py -k categories_and_export`
  - Result: skipped outside the compose runner as expected because `MEM0_E2E_BASE_URL` was unset locally.
- `PYTHONDONTWRITEBYTECODE=1 python scripts/run_live_e2e_compose.py`
  - Result: passed with exit code 0 from `/workspace/data/mem0/mem0-platform-sidecar/.worktrees/dashboard-overlay-phase1`.
  - Evidence: compose built Mem0 from the resolved upstream checkout, both live E2E tests passed (`2 passed`), and teardown removed the task-specific containers, network, and `postgres-data` volume.
- `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests/test_e2e_compose_harness.py tests/e2e/test_live_mem0_oss.py`
  - Result: `6 passed, 2 skipped`; the skips are the live tests outside compose.
- `python -m ruff check . --no-cache`
  - Result: passed.
- Resource cleanup check
  - `docker ps -a --format '{{.Names}}' | rg 'mem0-sidecar-e2e-'`
  - `docker volume ls --format '{{.Name}}' | rg 'mem0-sidecar-e2e-'`
  - `docker network ls --format '{{.Name}}' | rg 'mem0-sidecar-e2e-'`
  - Result: no matching containers, volumes, or networks remained.

Files changed
- `docker/docker-compose.e2e.yml`
- `scripts/run_live_e2e_compose.py`
- `tests/e2e/test_live_mem0_oss.py`
- `tests/test_e2e_compose_harness.py`
