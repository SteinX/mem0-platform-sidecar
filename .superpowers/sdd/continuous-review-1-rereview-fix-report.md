# Continuous Review 1 Re-review: Integrated P1/P2 Fix Report

Base reviewed: `b502a26`

Implementation commit: `a58e6aa` (`fix mutation recovery state machine`)

Scope: the isolated `dashboard-explorer-phase2` worktree. No submission or
change was made to the official `mem0ai/mem0` repository. The two P3 findings
remain out of scope, and the closed SQLite serialization and opaque-memory-ID
contracts were preserved.

## Outcome

All four validated P1/P2 findings in
`continuous-review-1-rereview-fix-brief.md` are implemented:

- Add identity is per logical operation. An optional, validated
  `Idempotency-Key` is scoped by project/app/operation and represented only by
  a SHA-256 operation marker. Unkeyed identical adds remain independent.
- One add response may own multiple memory IDs. Every bounded marked result is
  adopted and retained; recovery never treats a shared marker as a duplicate
  and never calls add or delete.
- Mutation intents now distinguish `ACTIVE`, `UNKNOWN`, `COMPLETED`, `FAILED`,
  `PARTIAL`, and `EXHAUSTED`. Recovery is bounded, observation-only, and blocks
  a new scoped mutation while an ambiguous outcome remains unresolved.
- Revisions 0005/0006 use validated one-statement compatibility snapshots, and
  revision 0007 refuses downgrade while any nonterminal intent remains.

Final verification passed the full Python suite, the retained PostgreSQL
smoke, SQLite serialization, route/repository/migration/opaque-ID regressions,
Ruff, diff and secret checks, and a fresh disposable applied-dashboard
typecheck plus all overlay harnesses. The aggregate Compose attempt stopped in
Docker Buildx before services started; exact cleanup was independently proven.

## Architecture and invariants

### Logical add identity and exact retry

`Idempotency-Key` is accepted only as 1-128 visible ASCII characters and is
validated in the route before project bootstrap or upstream access. The raw
key is never persisted. The marker hashes project ID, app ID, operation, and
the logical key; the request body has a separate canonical fingerprint.

The unique `(project_id, app_id, operation, operation_key)` constraint closes
the concurrent insert race. A completed exact retry returns the persisted,
sanitized result. A different payload, active claim, or unresolved/absent
outcome returns deterministic HTTP 409 and never issues a second add. Without
a key, a cryptographically random logical key makes identical payloads distinct.

One upstream add may return several IDs. All IDs are persisted as intent
targets and indexed. Add recovery lists a bounded upstream window, selects all
records with the exact operation marker and sidecar project/app scope, adopts
the complete result set, and performs zero upstream writes.

### Explicit observational state machine

New intents start `ACTIVE`, with attempt 1 and a lease, and are committed
before upstream I/O. Ordinary pre-effect lock/upstream errors become terminal
`FAILED`; explicit entity delete `FAILED`/`PARTIAL` remains terminal.
Cancellation and failures after an upstream response/effect become `UNKNOWN`.

Recovery uses at most three attempts. It commits the incremented `ACTIVE`
claim before any external read, reacquires the Project lock, and then observes:

- add: list by the exact marker; never call add/delete;
- update: GET the target and compare hashed requested-field effects;
- memory delete: GET the exact target; only a confirmed 404 converges locally;
- entity delete: GET every exact target; all absent converges, all present
  remains `UNKNOWN`, and mixed absent/present terminalizes as `PARTIAL` while
  tombstoning only confirmed-absent rows.

Observation failure preserves the committed attempt. Attempt three becomes
`EXHAUSTED`, which continues to block the scope. No recovery path invokes add,
update, delete, or bulk entity deletion.

### Lock and transaction ordering

Recovery preflight takes the Project mutation lock before listing recoverable
or blocking intents. This matters when request 1 has durably written an
`ACTIVE` intent and currently holds the mutation lock: request 2 waits, then
rereads the intent after request 1 commits instead of returning a stale 409.

When preflight finds no blocker, it intentionally returns without rollback;
the caller keeps that Project lock until its durable intent commit. The short
durable-intent-to-lock-reacquire window is fail-safe: a competitor may return
409, but it cannot bypass a live operation. Projection work retains the
`Project -> MemoryIndex -> Entity` order on SQLite and PostgreSQL.

The ownership sequence is:

1. preflight lock and blocker reread;
2. Event + `ACTIVE` intent + targets commit;
3. Project lock reacquisition and upstream mutation;
4. MemoryIndex + Entity + Event + terminal intent in one owning commit.

Recovery separately commits its claim before step 3's observation-only reads.

### Interruption-safe migrations

0005 and 0006 rebuild a stale compatibility artifact whenever source columns
still exist. `CREATE TABLE AS SELECT` creates the DATA snapshot atomically.
The migration validates source/data row counts and distinct durable IDs, then
adds exactly one READY row containing the validated count. Upgrade refuses an
invalid, incomplete, or missing-ready snapshot before restoration; it restores
only DATA rows and drops the artifact only after successful schema/data work.

0007 checks intent status before dropping any index or table. Only
`COMPLETED`, `FAILED`, and `PARTIAL` history may be discarded. `ACTIVE`,
`UNKNOWN`, legacy `PENDING`, `EXHAUSTED`, or any unknown future status causes
an explicit refusal while preserving both intent/target rows and the Alembic
revision.

## RED evidence

### Add identity, multi-result, and safe result reuse

```text
pytest tests/core/test_mutation_recovery.py \
  -k 'preserves_every_id or identical_adds or exact_key_retry or different_keys'
4 failed
```

The failures exposed destructive multi-result dedupe, shared content identity
for unkeyed identical adds, and missing client-key support.

```text
pytest tests/core/test_mutation_recovery.py \
  -k 'different_payload or isolated_by_project or unique_race or unknown_add_blocks'
3 failed, 1 passed

pytest tests/http_adapter/test_memory_routes.py \
  -k 'idempotency_key or reuses_completed_result'
4 failed

pytest tests/core/test_mutation_recovery.py -k safe_result
1 failed
```

These reproduced missing payload-conflict and unique-race handling, missing
route validation/reuse, and first-response versus retry redaction drift.

### State machine and safe HTTP conflicts

```text
pytest tests/core/test_mutation_recovery.py \
  -k 'lock_failure or known_upstream or cancelled_add or cancelled_update or \
      cancelled_delete or entity_recovery or claim_attempt or \
      recovery_exhaustion or legacy_pending'
11 failed, 19 deselected
```

The old code conflated known and unknown outcomes, rolled back claims, replayed
writes, and lacked exhaustion.

```text
pytest tests/http_adapter/test_memory_routes.py \
  -k 'unresolved_mutation_scope or reuses_completed_result'
3 failed, 2 passed
```

Update, delete, and entity conflicts surfaced as 500 instead of deterministic
409 responses.

### Migration interruption and downgrade safety

```text
pytest tests/store/test_migrations.py \
  -k 'interrupted_empty_snapshot or refuses_unresolved_rows'
3 failed
```

0005 lost trace data, 0006 restored the wrong app mapping, and 0007 dropped an
unresolved intent.

### Retained serialization regression

The first PostgreSQL smoke reached every migration check, then its update /
entity-delete interleaving returned `MutationConflictError('Scoped mutation
recovery is already in progress')`. The retained SQLite reproduction was:

```text
pytest tests/core/test_sqlite_mutation_serialization.py -k update
1 failed, 1 deselected
```

The second request read the live `ACTIVE` intent before waiting for request 1's
Project lock. Moving the preflight read under that lock produced the intended
wait-and-reread behavior on both databases.

## GREEN and verification evidence

### Focused recovery, route, and migration gates

```text
tests/core/test_mutation_recovery.py
32 passed

tests/core/test_memory_ops.py tests/core/test_entities.py \
  tests/core/test_mutation_recovery.py
260 passed

tests/http_adapter/test_memory_routes.py \
  -k 'unresolved_mutation_scope or reuses_completed_result'
5 passed, 71 deselected

tests/store/test_migrations.py
22 passed

tests/core/test_sqlite_mutation_serialization.py \
  tests/core/test_mutation_recovery.py
34 passed
```

The combined repository/route/entity/recovery/migration/opaque-ID matrix first
reported 498 passed and one lock-order assertion expecting one Project lock.
The new invariant intentionally has a recovery-preflight lock and a durable
intent execution lock. After correcting only that expectation, the exact test
plus full serialization/recovery matrix reported 35 passed.

### Retained PostgreSQL smoke

The smoke used a temporary `CREATEDB` role against pgvector PostgreSQL and
removed the role/database in traps:

```text
PostgreSQL smoke passed: 0004->head, interruption-safe exact
downgrade/re-upgrade, 0007 unresolved-intent refusal, ORM/data checks,
update/delete and reconcile/delete serialization
```

It precreated stale empty 0005/0006 artifacts, proved rebuild and exact
roundtrip, inserted an `UNKNOWN` intent plus target, proved downgrade refusal
left tables/rows/revision intact, terminalized it, and proved normal downgrade
and re-upgrade.

### Full suite and static checks

The first fresh full run reported 942 passed, 5 skipped, and one static harness
failure because the PostgreSQL script now has three intentional head upgrades
rather than two. The harness was updated to require three plus the explicit
0007 guard. The mandatory fresh rerun was:

```text
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
943 passed, 5 skipped, 1 warning in 146.38s

python -m ruff check .
All checks passed!

git diff --check
<no output>
```

The warning is the unchanged Starlette `TestClient`/httpx deprecation. A
high-confidence added-line credential scan found no private key, AWS key,
GitHub token, or OpenAI-style secret. Broad matches were reviewed as Python's
`secrets` module, synthetic redaction fixtures, `.invalid`/`.local` hosts, and
the existing compose-internal `http://mem0:8000` test URL.

There are no changed TS/TSX/CJS/JSON/YAML files in this wave, so the established
changed-frontend Prettier gate is not applicable. A whole disposable dashboard
check reported 18 pre-existing formatting warnings in untouched upstream/base
overlay files; those were not expanded into this P1/P2 fix scope.

### Fresh applied dashboard

A clean temporary upstream dashboard received the current overlay and linked
the existing dependency installation:

```text
tsc --noEmit
sidecar proxy request harness: 42 contracts passed
category schema harness: 12 contracts passed
category editor state harness: 9 contracts passed
explorer query state harness: 11 contracts passed
explorer components harness: 11 contracts passed
memory explorer state harness: 11 contracts passed
request trace state harness: 5 contract groups passed
```

The shared applied checkout was excluded from candidate evidence because two
files (`entities/page.tsx`, `sidecar-proxy.ts`) were stale relative to this
worktree. Its typecheck passed, but its older proxy copy failed the current
harness. The fresh applied candidate above is the authoritative overlay result.

### Aggregate Compose diagnostic and cleanup

The single standard Compose attempt stopped during `compose up --build`, before
any service or application assertion. Docker Buildx canceled concurrent image
exports during layer import and panicked:

```text
panic: send on closed channel
github.com/docker/buildx/util/progress.(*Printer).Write
github.com/docker/buildx/util/dockerutil.(*Client).LoadImage
```

Compose diagnostics showed no created services. The runner's `finally` cleanup
ran, and an independent audit of exact project
`mem0-sidecar-e2e-404500-595a38d4` found:

```text
containers=0
networks=0
volumes=0
images=0
processes=0
```

No matching temporary dashboard directory remained. Per the brief, this
pre-application BuildKit infrastructure failure was not retried and did not
weaken the independently green PostgreSQL, overlay, route, and full-suite gates.

## Files changed

- Schema/migrations: `models.py`, migrations 0005, 0006, and 0007.
- Store/service: `repositories.py`, `memory_ops.py`, `entities.py`.
- HTTP: `app.py`, `memory_routes.py`.
- Live gate: `run_postgres_migration_smoke.py`.
- Regressions: mutation recovery, memory operation/route, migration, and E2E
  compose-harness tests.

## Concerns

- The aggregate runner remains exposed to a Docker Buildx progress/import panic
  on this host. It failed before application startup and left zero exact-project
  resources; all affected code paths passed independently.
- The unchanged Starlette/httpx warning remains outside this fix wave.
- Whole-dashboard Prettier has 18 untouched baseline warnings. No frontend file
  changed in this wave, and the fresh applied overlay typecheck/harnesses passed.
