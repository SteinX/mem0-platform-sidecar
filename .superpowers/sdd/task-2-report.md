# Task 2 Report: Export Job Model and Repository

## Scope

Implemented Task 2 in the requested write scope:

- `src/mem0_sidecar/store/models.py`
- `src/mem0_sidecar/store/repositories.py`
- `migrations/versions/0002_export_jobs.py`
- `tests/store/test_models.py`
- `tests/store/test_repositories.py`

## RED Evidence

Added the repository tests from the task brief first, then ran:

```bash
python -m pytest tests/store/test_repositories.py::test_export_job_repository_lifecycle tests/store/test_repositories.py::test_export_job_repository_get_is_project_scoped -q
```

Observed failure before production changes:

- `ImportError: cannot import name 'ExportStatus' from 'mem0_sidecar.store.models'`

This was the expected missing-feature failure proving the tests were exercising unimplemented Task 2 behavior.

## GREEN Evidence

After implementing `ExportStatus`, `ExportJob`, `ExportJobRepository`, and Alembic migration `0002_export_jobs`, the same focused repository tests passed:

```bash
python -m pytest tests/store/test_repositories.py::test_export_job_repository_lifecycle tests/store/test_repositories.py::test_export_job_repository_get_is_project_scoped -q
```

Result:

- `2 passed in 0.26s`

Also verified the touched store tests:

```bash
python -m pytest tests/store/test_models.py tests/store/test_repositories.py -q
```

Result:

- `13 passed in 0.15s`

Verified SQLite Alembic upgrade/downgrade:

```bash
tmpdb="$(mktemp -u /tmp/mem0-sidecar-export-XXXX.sqlite3)"
MEM0_SIDECAR_DATABASE_URL="sqlite:///$tmpdb" python -m alembic upgrade head
MEM0_SIDECAR_DATABASE_URL="sqlite:///$tmpdb" python -m alembic downgrade base
rm -f "$tmpdb"
```

Result:

- upgrade `0001_control_plane_core -> 0002_export_jobs` succeeded
- downgrade `0002_export_jobs -> 0001_control_plane_core` succeeded

Verified targeted lint:

```bash
python -m ruff check src/mem0_sidecar/store tests/store migrations/versions/0002_export_jobs.py
```

Result:

- `All checks passed!`

Verified broader suite:

```bash
python -m pytest -q
```

Result:

- `88 passed, 1 skipped, 1 warning in 5.36s`

Warning observed:

- existing `StarletteDeprecationWarning` from `fastapi.testclient` / `starlette.testclient`

## Files Changed

- `src/mem0_sidecar/store/models.py`
  - added `ExportStatus`
  - added `ExportJob`
- `src/mem0_sidecar/store/repositories.py`
  - added `ExportJobRepository`
- `migrations/versions/0002_export_jobs.py`
  - added `export_jobs` table migration
  - guarded named enum create/drop for non-SQLite binds only
- `tests/store/test_models.py`
  - added ORM persistence coverage for `ExportJob`
- `tests/store/test_repositories.py`
  - added export job lifecycle and project-scoped lookup tests

## Self-Review

- Stayed within Task 2 write scope.
- Used TDD: tests added first, verified failing before implementation.
- Preserved Task 1 work at HEAD `ca71d56`; no revert of prior category admin API changes.
- Kept export job persistence sidecar-only; no data-plane changes to Mem0 OSS.
- Followed the cross-dialect downgrade requirement by dropping the named enum only on non-SQLite binds after dropping the dependent table.
- The repository implements only the Task 2 methods from the brief with minimal behavior needed by the tests.

## Review Fix Addendum

Addressed the Task 2 review findings in the requested store and migration scope:

- `ExportJobRepository.mark_running`, `mark_succeeded`, and `mark_failed` now require `(project_id, job_id)` and resolve jobs through the existing project-scoped `get(...)` path.
- Export lifecycle tests now reload persisted state after commit/expire instead of asserting only against the same in-memory ORM instance.
- Added explicit `mark_failed(...)` and `list_project_exports(...)` coverage.
- Added SQLite Alembic upgrade/downgrade coverage for `0002_export_jobs`, including a raw migrated-DB insert that proves the migration supplies the runtime defaults expected by the ORM/store.
- Aligned `ExportJob` ORM and `0002_export_jobs` migration with server defaults for status, JSON payload columns, and count columns so upgraded databases behave consistently with Task 2 runtime expectations.

### Review Fix RED Evidence

First repository red run after changing the lifecycle API in tests:

```bash
python -m pytest tests/store/test_repositories.py::test_export_job_repository_lifecycle tests/store/test_repositories.py::test_export_job_repository_lifecycle_is_project_scoped tests/store/test_repositories.py::test_export_job_repository_failed_lifecycle_and_listing_reload_from_database -q
```

Observed expected failure before repository implementation changes:

- `TypeError: ExportJobRepository.mark_running() takes 2 positional arguments but 3 were given`
- `TypeError: ExportJobRepository.mark_failed() takes 2 positional arguments but 3 positional arguments ... were given`

Then added the migration-backed drift test and ran:

```bash
python -m pytest tests/store/test_migrations.py::test_export_jobs_migration_supports_runtime_defaults_and_downgrade -q
```

Observed expected schema failure before migration/model alignment:

- `sqlite3.IntegrityError: NOT NULL constraint failed: export_jobs.status`

This proved the upgraded SQLite schema could not persist an export job using the runtime defaults implied by the Task 2 ORM/store behavior.

### Review Fix GREEN Evidence

Repository lifecycle tests after implementation:

```bash
python -m pytest tests/store/test_repositories.py::test_export_job_repository_lifecycle tests/store/test_repositories.py::test_export_job_repository_lifecycle_is_project_scoped tests/store/test_repositories.py::test_export_job_repository_failed_lifecycle_and_listing_reload_from_database -q
```

Result:

- `3 passed in 0.07s`

Migration-backed test after aligning ORM + Alembic defaults:

```bash
python -m pytest tests/store/test_migrations.py::test_export_jobs_migration_supports_runtime_defaults_and_downgrade -q
```

Result:

- `1 passed in 0.21s`

Focused verification:

```bash
python -m pytest tests/store/test_models.py tests/store/test_repositories.py tests/store/test_migrations.py -q
python -m ruff check src/mem0_sidecar/store tests/store migrations/versions/0002_export_jobs.py
tmpdb="$(mktemp -u /tmp/mem0-sidecar-export-XXXX.sqlite3)"
MEM0_SIDECAR_DATABASE_URL="sqlite:///$tmpdb" python -m alembic upgrade head
MEM0_SIDECAR_DATABASE_URL="sqlite:///$tmpdb" python -m alembic downgrade base
rm -f "$tmpdb"
```

Results:

- `16 passed in 0.92s`
- `All checks passed!`
- Alembic SQLite `upgrade head` and `downgrade base` both succeeded

Broader verification:

```bash
python -m pytest -q
```

Result:

- `91 passed, 1 skipped, 1 warning in 4.57s`

Remaining warning:

- existing `StarletteDeprecationWarning` from `fastapi.testclient` / `starlette.testclient`
