# Dashboard Explorer Phase 2 Final Fix Report

Baseline reviewed: `db620a3`

Implementation range: `db620a3..e7c5aca`

Scope: the isolated `dashboard-explorer-phase2` worktree only.

## Outcome

All six Important findings in `phase2-final-review-findings.md` are resolved.
The final full suite, applied-overlay verifier, disposable PostgreSQL smoke,
real-browser smoke, and integrated live Compose runner passed. The successful
Compose project was removed with no remaining containers, networks, volumes,
or project images.

## RED evidence and fixes

### 1. Consistent mutation lock order

At the baseline, memory update and reconcile could mutate `MemoryIndex` and
then acquire the entity rebuild's `Project` lock. Entity deletion did the
reverse. A discriminating call-order regression failed because update and
reconcile reached upstream/`MemoryIndex` work before the project lock.

The fix adds `ProjectRepository.lock_for_mutation`, a PostgreSQL `FOR UPDATE`
lock, and makes every projection mutation follow one order:

`Project -> upstream mutation/read -> MemoryIndex -> Entity`

The project lock is acquired before the upstream add/update/delete or
reconcile read and held through the local projection transaction. Acquiring it
only immediately before the local write was insufficient: entity deletion
could otherwise finish while an already-started memory operation retained a
stale upstream result and later resurrected the projection. Query hydration's
stale cleanup also locks the project and uses
`mark_stale_if_unchanged(updated_at_lte=hydration_cutoff)` so a concurrent fresh
projection is not marked stale.

The retained real-PostgreSQL smoke runs update/delete and reconcile/delete
interleavings. Both complete without deadlock, and the final `MemoryIndex` and
`Entity` state proves deletion is not resurrected.

### 2. Bounded Memory Explorer query work

Baseline hostile tests showed that 65 filters, 101 `in` members, 257-character
values/metadata keys, and page windows beyond 5,000 records were accepted. A
high-page regression also showed the service requesting a page-one candidate
scan sized by `offset + candidate_limit`. The initial boundary group had six
expected RED failures while the exact legal boundary remained accepted.

The central parser now enforces:

- at most 64 filters;
- at most 100 members per `in` value;
- at most 256 characters for scalar values and metadata keys/values;
- `page * page_size <= 5,000`.

The repository now accepts an exact bounded `window_offset` and `window_limit`.
The service requests only the target page plus the 20-record hydration buffer,
never the offset-sized prefix. Scalar queries retain SQL `COUNT`, ordering,
date/filter semantics, and exact totals. Metadata filtering retains its
bounded 5,000-record exact-match scan and returns the exact total before taking
the requested window. Hostile, legal-boundary, high-page, stale-hydration, and
exact-total regressions are retained.

### 3. Entity activity date

The baseline Entity UI preferred `updated_at`, so an administrative rebuild
could make an inactive identity appear recently active. Both desktop and
mobile now use the shared runtime helper:

`entity.last_seen_at ?? entity.updated_at`

The explorer component harness supplies deliberately different timestamps and
asserts that `last_seen_at` wins, with `updated_at` retained only as the null
fallback.

### 4. Optional Request Trace app configuration

At the baseline, the proxy-to-route harness returned 500 when
`SIDECAR_APP_ID` was unset even though the documented behavior delegates to the
project's server-owned `default_app_id`.

The proxy now requires and validates the configured project ID while treating
the configured app ID as optional. When it is absent, the proxy omits
`app_id` from GET query scope and POST JSON scope so the sidecar resolver picks
the project default. When configured, the exact app ID is propagated. Empty,
overlong, control-character, and non-portable configured IDs still fail
closed. The route-level Node harness retains both optional and configured
modes and reports 41 contracts passed.

### 5. Retained PostgreSQL migration smoke

`scripts/run_postgres_migration_smoke.py` creates a uniquely named disposable
database in the live PostgreSQL service, migrates through 0004, seeds
representative legacy event/entity data, and proves:

- 0005 event backfills, constraints, and indexes;
- 0006 project/app entity scoping and deterministic duplicate collapse;
- ORM-to-schema column parity and post-migration read/write usability;
- downgrade to 0004 and re-upgrade through 0005/0006;
- update/delete and reconcile/delete serialization without deadlock or stale
  entity resurrection.

The e2e runner image includes Alembic, migrations, the smoke script, source,
tests, and the `postgres` optional dependency. The live runner executes the
smoke before API/browser checks and always drops the temporary database.

### 6. Retained real-browser interaction smoke

The browser gate uses the existing Node runtime and raw Chrome DevTools
Protocol against the overlay applied to a temporary copy of the upstream
dashboard. It uses the already available Chromium image and adds no dashboard
package, JavaScript test framework, or lockfile dependency.

The retained 34 assertions cover desktop and narrow viewports, including:

- loading, empty, error, and recovered result states;
- filter and date popovers;
- memory and request detail drawers with real mocked detail content;
- trusted keyboard Space activation for the request action and narrow memory
  row;
- slow-search/fast-add replacement so stale deferred work cannot overwrite the
  latest result;
- focus restoration after close;
- memory and entity typed destructive confirmation;
- settled drawer geometry and root/body horizontal overflow.

The live runner applies the production overlay files to a temporary dashboard
context. It then copies only
`tests/e2e/dashboard_client_layout.browser-smoke.tsx` into that disposable
context to bypass the unrelated upstream auth guard for the browser test. This
test-only shell is never part of the overlay manifest or production output.
The first integrated run also exposed a cold Next route compilation completing
at the original 15-second boundary. A RED harness contract and a narrowly
scoped 30-second allowance for the first Entity route compile fixed that
runner race; the complete rerun passed.

## Verification evidence

### Focused Python regressions

```text
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/core/test_explorer_filters.py \
  tests/core/test_memory_ops.py \
  tests/store/test_repositories.py
206 passed in 4.83s
```

This group includes the exact lock-order, hostile bounds, legal boundary,
high-page bounded-window, exact-total, and conditional stale-marking tests.

```text
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/test_dashboard_overlay_scripts.py \
  tests/test_e2e_compose_harness.py
152 passed in 36.02s
```

This group includes the Entity activity runtime contract, optional/configured
app proxy-to-route contracts, retained PostgreSQL/browser runner contracts,
applied-overlay wiring checks, cleanup checks, and the cold-route regression.

### Full suite and lint

```text
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q
886 passed, 5 skipped, 1 warning in 186.29s (0:03:06)

python -m ruff check .
All checks passed!

git diff --check
<no output>
```

The one warning is the existing Starlette `TestClient`/httpx deprecation; it
does not affect the result.

### Applied overlay verifier

The shared checkout at `/workspace/data/mem0/upstream/server/dashboard` still
contained an older applied overlay, so its first proxy harness correctly did
not match the current worktree. Per the worktree-only constraint, it was not
mutated. A fresh disposable copy was made, current overlay files were applied,
the existing `node_modules` was linked, and the required verifier was run:

```text
python integrations/mem0-dashboard-overlay/scripts/apply-dashboard-overlay \
  /tmp/phase2-final-applied-dashboard
python integrations/mem0-dashboard-overlay/scripts/verify-dashboard-overlay \
  /tmp/phase2-final-applied-dashboard

> tsc --noEmit
sidecar proxy request harness: 41 contracts passed
category schema harness: 12 contracts passed
category editor state harness: 9 contracts passed
explorer query state harness: 11 contracts passed
explorer components harness: 11 contracts passed
memory explorer state harness: 11 contracts passed
request trace state harness: 5 contract groups passed
```

The disposable applied checkout was removed afterward.

### Disposable PostgreSQL smoke

Focused and integrated invocation:

```text
docker compose -f docker/docker-compose.e2e.yml -p <isolated-project> run \
  --rm --no-deps e2e-runner python \
  /app/scripts/run_postgres_migration_smoke.py \
  --database-url=postgresql+psycopg://postgres:e2e-postgres@postgres/postgres

PostgreSQL smoke passed: 0004->0005->0006, downgrade/re-upgrade, ORM/data checks, update/delete and reconcile/delete serialization
```

### Integrated live Compose and real browser

```text
PYTHONDONTWRITEBYTECODE=1 python scripts/run_live_e2e_compose.py

PostgreSQL smoke passed: 0004->0005->0006, downgrade/re-upgrade, ORM/data checks, update/delete and reconcile/delete serialization
11 passed, 1 deselected, 1 warning in 4.12s
1 passed, 11 deselected, 1 warning in 1.07s
Browser smoke passed: 34 behavior assertions across desktop and narrow viewports
exit code 0
```

Successful project: `mem0-sidecar-e2e-60668-706d334f`.

The runner's `finally` block executes:

```text
docker compose ... down -v --remove-orphans --rmi local
```

The runner's own cleanup assertion passed. Independent post-run checks also
returned no output for all four resource types:

```text
docker ps -a --filter label=com.docker.compose.project=mem0-sidecar-e2e-60668-706d334f
docker network ls --filter label=com.docker.compose.project=mem0-sidecar-e2e-60668-706d334f
docker volume ls --filter label=com.docker.compose.project=mem0-sidecar-e2e-60668-706d334f
docker image ls --filter reference=mem0-sidecar-e2e-60668-706d334f-\*
```

Residue: 0 containers, 0 networks, 0 volumes, 0 project images.

## Self-review

- Locking: every projection mutation now has a shared project serialization
  root before upstream/local mutation, and PostgreSQL proves the two requested
  interleavings. No reverse `MemoryIndex -> Project` path remains in the
  reviewed mutation flows.
- Bounds: parser limits are central, exact legal boundaries pass, hostile
  values fail before repository work, and high-page queries load only the
  requested bounded window plus hydration buffer.
- Activity: desktop and narrow layouts call one helper whose runtime test
  discriminates `last_seen_at` from `updated_at`.
- Trace scope: unset app is omitted, configured app is exact, and portable-ID
  validation remains fail-closed.
- PostgreSQL: the retained script covers both migrations and concurrency on a
  real disposable server and is invoked by the standard live runner.
- Browser: the retained behavior test runs current applied overlay code at two
  viewports without a new dashboard dependency/framework and has deterministic
  cleanup.
- Repository hygiene: `git diff --check` and Ruff pass; the successful live
  project has zero Docker residue.

No unresolved code finding remains. The stale shared applied dashboard checkout
was intentionally left unchanged because it is outside this worktree's change
scope; fresh current application and verification passed.

## Implementation commits

- `6c8432d` `fix: bound explorer mutations and queries`
- `199366b` `fix: align explorer activity and trace scope`
- `a00b164` `fix: serialize projection mutations before upstream`
- `e9a60c8` `test: retain postgres and browser explorer smokes`
- `e7c5aca` `test: tolerate cold entity route compile`

This report is committed separately as the final audit artifact.
