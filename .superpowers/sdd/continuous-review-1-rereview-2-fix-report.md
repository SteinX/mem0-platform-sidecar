# Continuous Review 1 Re-review 2 Fix Report

Status: `DONE_WITH_CONCERN`

Base: `1213aa466753163c021f269f1c9c29179df44c3c`

Implementation commit: `6c4dfac250cc2c6f6ca6b52a77f896bae8d7c180`

Report/ledger commit: this commit

The four validated P1/P2 blockers in
`/tmp/dashboard-explorer-phase2-continuous-review-1-rereview-2.md` are fixed.
The two retained P3 findings remain intentionally unchanged. The aggregate
Compose run is not reported as green: its PostgreSQL, live OSS, and adoption
stages passed, but the browser stage timed out as described under Concerns.

## Implemented fixes

### Ambiguous upstream outcomes become UNKNOWN

- `Mem0UpstreamError` now carries an explicit `outcome_unknown` classification.
  Status-bearing HTTP failures are terminal; status-less transport/read errors
  are ambiguous.
- Successful 2xx responses that cannot be decoded are wrapped as ambiguous
  upstream errors. Their body is neither retained on the exception nor included
  in the message or structured log fields.
- Add, update, delete, and entity delete record that the upstream call was
  attempted immediately before awaiting it. Lock and local validation failures
  before the call remain terminal.
- Ambiguous entity-target failure aborts the operation. Earlier uncommitted
  target/projection changes roll back, the intent remains `UNKNOWN`, and exact
  GET-only recovery converges the whole target set without replaying a write.
- Existing observation-only recovery, bounded attempts, idempotent add adoption,
  status-bearing 5xx handling, and pre-call failure handling are preserved.

### Recovered branch retains the Project lock

After processing recoverable intents, the service now rolls back the recovery
transaction, reacquires the Project mutation lock, rereads blockers under that
lock, and snapshots blocker statuses. It rolls back before returning a conflict,
but returns without rollback when the scope is clear. The caller therefore owns
the lock through its Event/intent/target commit, matching the empty-preflight
branch.

A real two-session SQLite regression pauses the final blocker read and attempts
a competing same-scope Project lock plus ACTIVE intent commit. The competing
commit cannot enter the check-to-intent gap, and request A performs no upstream
I/O until its own intent is durably committed.

### Exact b502a26 compatibility artifacts upgrade safely

Migrations 0005 and 0006 now recognize two formats only:

- the current exact DATA+READY schema; or
- the exact legacy schema emitted by `b502a26`, including columns, lengths,
  primary key, nullability, types, and absence of defaults.

Legacy IDs must be non-null, unique, and refer to current source rows; the 0006
legacy app ID is non-null. A non-empty complete legacy snapshot restores the
matching old rows while downgraded-era source rows receive normal new defaults.
An empty legacy snapshot is accepted only when its source table is also empty.
Partial, superset, constraint-lookalike, orphaned, and ambiguous empty artifacts
fail before adding columns or dropping the artifact. The artifact is removed
only after restore, constraints, and indexes complete successfully.

The retained PostgreSQL smoke converts current READY artifacts into the exact
legacy b502a26 schemas at revision 0004 and proves re-upgrade.

### 0007 downgrade guard is atomic with DROP

- SQLite executes
  `UPDATE mutation_intents SET updated_at = updated_at WHERE 0 = 1` before the
  unresolved count, acquiring a RESERVED write lock held through both drops.
- PostgreSQL executes
  `LOCK TABLE mutation_intents, mutation_intent_targets IN ACCESS EXCLUSIVE MODE`
  before the count, in application parent-before-target order.

Real two-connection SQLite regressions cover both orderings. An earlier writer
commits first, after which downgrade observes ACTIVE state and refuses. When the
migration gets the lock first, a later writer cannot commit an intent between
the guard and the drops and be silently discarded.

## RED-GREEN evidence

### Outcome classification and observational convergence

The initial outcome/terminal matrix was run with:

```text
python -m pytest -q \
  tests/mem0_client/test_client.py \
  tests/core/test_mutation_recovery.py \
  -k 'classifies_statusless or wraps_2xx or logs_and_raises or real_http_lost_response or lock_failure_before_delete or known_upstream_500'
```

RED reported 16 failures: `outcome_unknown` did not exist, successful invalid
JSON escaped as `JSONDecodeError`, and entity delete swallowed the ambiguous
target failure. A separate two-target entity subset reported two `DID NOT
RAISE` failures after the first target had already succeeded.

GREEN for the same outcome/terminal selection reported 18 passed. The real
stateful HTTP matrix covers add/update/delete/entity delete crossed with read
timeout, disconnect, and 2xx invalid JSON; each operation persisted `UNKNOWN`
and converged with GETs only. Status-bearing 5xx and pre-call Project-lock
failure remained terminal and issued no replay.

### Recovery lock retention

```text
python -m pytest -q \
  tests/core/test_sqlite_mutation_serialization.py::test_sqlite_recovery_branch_holds_project_lock_through_caller_intent_commit
```

RED failed with the competing intent committed in the recovery-to-caller gap.
GREEN passed after the locked final reread. The combined recovery and SQLite
serialization gate then reported 47 passed in 13.82 seconds.

### Legacy compatibility

```text
python -m pytest -q tests/store/test_migrations.py -k b502a26
```

RED reported 6 failures because the READY-only validator rejected exact old
tables. The first GREEN reported 6 passed and the full migration file reported
28 passed. Two added downgraded-era-row cases then failed RED with `invalid
legacy content`; after allowing source supersets while requiring every snapshot
ID to match, the b502a26 selection reported 8 passed. Orphan snapshot/source
tests remained fail-safe. Two exact-column constraint-lookalike tests next
failed RED with `DID NOT RAISE`, then passed after exact PK/nullability/type/
default validation. The final full migration coverage is included in the full
suite below.

### Atomic 0007 downgrade

```text
python -m pytest -q \
  tests/store/test_migrations.py::test_mutation_downgrade_waits_for_earlier_writer_then_refuses \
  tests/store/test_migrations.py::test_mutation_downgrade_lock_prevents_later_writer_from_being_dropped
```

Both real SQLite orderings failed RED at the unsafe interleaving assertion and
passed GREEN in 1.32 seconds after adding the database locks. The migration
file then reported 32 passed at that checkpoint; later exact-schema additions
are covered by the final full run.

## Verification

### Focused and full Python gates

The focused verification was split to keep output bounded:

```text
real client + recovery + memory operations + entities
296 passed in 17.19s

memory routes + entity routes
140 passed, 1 known warning in 36.97s

repositories + migrations + SQLite serialization + Compose harness
139 passed in 16.04s
```

Total focused result: 575 passed.

```text
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
971 passed, 5 skipped, 1 warning in 121.56s

python -m ruff check .
All checks passed!

git diff --check
<no output>
```

The warning is the unchanged Starlette `TestClient`/httpx deprecation. A
high-confidence added-line scan found no private key, AWS key, GitHub token, or
OpenAI-style token. `uv run` generated an untracked `uv.lock`; it was removed
before commit and is absent from the final status.

### PostgreSQL smoke

The final retained smoke used the existing PostgreSQL service through
`172.26.0.1:38432` and passed after the exact-schema validators were complete:

```text
PostgreSQL smoke passed: 0004->head, interruption-safe and b502a26-legacy
exact downgrade/re-upgrade, 0007 locked unresolved-intent refusal, ORM/data
checks, update/delete and reconcile/delete serialization
```

An initial global-Python invocation lacked `psycopg`, and a first host address
of `127.0.0.1:38432` refused the connection. These were invocation/environment
issues; the supported `uv --extra postgres` run against `172.26.0.1` passed.

### Fresh applied dashboard overlay

A clean archive of upstream dashboard HEAD received the current overlay, linked
the existing dependency installation, and passed:

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

This fix wave has no frontend diff.

### One bounded aggregate Compose attempt

Docker was healthy before the attempt. The exact project was
`mem0-sidecar-e2e-471549-5752732a`. Within the single permitted attempt:

```text
PostgreSQL migration/serialization smoke: passed
live OSS main runner: 11 passed, 1 deselected
dedicated adoption runner: 1 passed, 11 deselected
browser smoke: failed
```

The browser failure was `Timed out waiting for "Memory details"`. Its captured
body showed both memory rows plus the open July/August date-range popover. The
request log showed RSC navigation to `memoryId=mem-1`, but no sidecar memory
detail fetch occurred before the timeout. The runner was not retried, and no
frontend code was changed in this wave.

The runner's `finally` cleanup completed. Independent exact-project audit found:

```text
containers=0
networks=0
volumes=0
images=0
matching processes=0
temporary dashboard directories=0
```

## Self-review

- Ambiguity is derived from an explicit client classification and only matters
  after the service records that the await was attempted; local failures cannot
  become `UNKNOWN` accidentally.
- Invalid success bodies are not copied into exceptions or structured logs.
- The entity multi-target test proves earlier local changes roll back and exact
  read-only recovery handles the full target set.
- The recovered branch has a deliberate rollback before reacquiring the lock,
  no rollback on its clear return path, and a rollback before every conflict.
- Legacy validation allows safe source supersets but rejects any snapshot row
  with no source match. Validation precedes column addition and artifact drop.
- PostgreSQL uses application-compatible table lock order; SQLite obtains its
  write lock before the count. Both locks remain inside the Alembic transaction.

## Concerns

- Aggregate Compose is not fully green. The browser interaction opened or left
  open the date-range popover and never issued the detail fetch before timeout.
  This is isolated from this backend/migration-only diff; fresh applied-overlay
  typecheck and all seven static harnesses passed. Per the brief it was not
  retried and no frontend fix was attempted.
- The two retained P3 findings from the review remain intentionally out of
  scope.
- The unchanged Starlette/httpx deprecation warning remains.
