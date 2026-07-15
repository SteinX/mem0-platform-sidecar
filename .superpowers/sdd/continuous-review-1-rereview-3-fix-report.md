# Continuous Review 1 Re-review 3 Fix Report

Status: `DONE_WITH_CONCERN`

Base: `fba713da209832602462a71ffd7bae5709a2d38f`

Implementation commit: `c7e4c39df2c865c6b281f42799822e192a7bff4e`

Report/ledger commit: this commit

Both P2 blockers from
`/tmp/dashboard-explorer-phase2-continuous-review-1-rereview-3.md` are fixed.
The two retained P3 findings remain intentionally unchanged. This backend and
migration-only wave did not rerun the overlay or aggregate Compose browser gate,
as required by the fix brief; the prior browser concern remains recorded below.

## Implemented fixes

### Every ordinary successful-response decode failure is ambiguous

- `Mem0RestClient` now catches every ordinary `Exception` raised by
  `response.json()` while still allowing `BaseException` subclasses to escape.
  The wrapper is `Mem0UpstreamError(outcome_unknown=True)` and retains neither
  the response body nor the synthetic secret embedded in the deep JSON fixture.
- Once add, update, delete, or entity delete has marked its upstream await as
  attempted, an escaping exception is ambiguous unless it is the exact
  `Mem0UpstreamError` type with an explicitly readable
  `outcome_unknown=False` classification.
- Exact classified HTTP rejection remains terminal, as do Project-lock and
  other local failures before the upstream await. Subclasses and hostile or
  unreadable classifications fail safe to ambiguous.
- Entity delete now aborts immediately on an ambiguous target failure. Earlier
  local target/projection changes roll back, and observation-only recovery
  converges the complete target set without replaying a write.
- Stateful production-client tests cover applied add, update, delete, and
  entity delete followed by a 20,000-level syntactically valid JSON response.
  Independent raw `RecursionError` defense tests cover all four services. Both
  matrices prove `UNKNOWN`, GET/list-only convergence, unchanged write counts,
  and entity-local rollback.

### Legacy compatibility tables require exact dialect descriptors

- Migrations 0005 and 0006 compare the complete ordered reflected descriptor
  for the supported SQLite and PostgreSQL dialects: column name, exact
  dialect-reflected type name and string length, nullability, default, and
  primary-key membership/order.
- Validation also requires no unexpected indexes, unique constraints, foreign
  keys, or checks. SQLite's inspector omits inline `UNIQUE` auto-indexes, so the
  validator additionally reads `PRAGMA index_list` and permits only the
  primary-key auto-index origin.
- Positive SQLite legacy fixtures are constructed from the original
  SQLAlchemy `Table` and `Column` definitions and assert their full reflected
  descriptors and empty unexpected-schema collections.
- Negative fixtures reject 0005 `REAL`/`SMALLINT`, changed identifier
  nullability, and reordered columns; reject 0006 `CHAR`/`NVARCHAR` and
  reordered columns; and reject added index, unique, foreign-key, and check
  objects for both versions.
- Existing non-empty restore, downgraded-era new-row defaults,
  ambiguous-empty rejection, orphan rejection, READY validation, cleanup
  ordering, and PostgreSQL exact legacy downgrade/re-upgrade remain green.

## RED-GREEN evidence

### Decode ambiguity

The required test-only RED checkpoint reported:

```text
9 failed, 12 passed
```

The failures were the raw client `RecursionError`, add/update/delete services
terminalizing the applied mutation, and entity delete swallowing the ambiguous
target exception. After the production change, the identical focused selection
reported:

```text
21 passed in 2.65s
```

### Exact legacy descriptors

The required test-only RED checkpoint reported:

```text
12 failed, 2 passed
```

Every new type, nullability, ordering, index, unique, foreign-key, and check
lookalike was incorrectly accepted. The first GREEN pass isolated SQLite's
hidden inline-unique auto-index behavior; after the dialect-aware PRAGMA check,
the identical selection reported:

```text
14 passed in 0.53s
```

## Verification

### Focused and full Python gates

```text
client + mutation recovery + entities + migrations + SQLite serialization
251 passed in 22.20s

final fresh full suite
993 passed, 5 skipped, 1 warning in 137.10s

python -m ruff check .
All checks passed!

git diff --check
<no output>
```

The warning is the unchanged Starlette `TestClient`/httpx deprecation. During
the first full run, three old tests used ordinary `RuntimeError` as a known
upstream rejection. They were updated to exact
`Mem0UpstreamError(outcome_unknown=False)` fixtures, while the retained
pre-attempt Project-lock `RuntimeError` test remains terminal. The mandatory
fresh full rerun above passed.

A high-confidence added-line scan found no private key, AWS key, GitHub token,
or OpenAI project token. Broader matches were reviewed as synthetic secret
redaction fixtures. The generated untracked `uv.lock` was removed before the
implementation commit.

### PostgreSQL smoke

The retained live PostgreSQL migration and serialization runner passed against
the existing `mem0-dev-postgres-1` service using its current runtime credentials
without printing or persisting them:

```text
PostgreSQL smoke passed: 0004->head, interruption-safe and b502a26-legacy
exact downgrade/re-upgrade, 0007 locked unresolved-intent refusal, ORM/data
checks, update/delete and reconcile/delete serialization
```

An initial invocation used a stale password from an older report and failed
authentication before creating its disposable database. The successful run
read current credentials into shell variables only; this was an environment
invocation correction, not a code change or application failure.

## Self-review

- The new client catch is scoped to `response.json()` and catches `Exception`,
  never `BaseException`.
- Service ambiguity is gated by `upstream_attempted`; no local/lock exception
  before the await can be promoted to `UNKNOWN`.
- Only the exact explicit-false upstream classification is terminal. The
  retained status-bearing 5xx, statusless explicit-false, guarded status-value,
  and pre-call lock controls pass.
- Recovery remains observational and does not replay add, update, delete, or
  entity-delete writes.
- Legacy descriptors are ordered and dialect-specific. SQLite auto-index
  visibility is handled explicitly, while PostgreSQL uses inspector-provided
  indexes and constraints.
- No restore SQL, READY marker validation, artifact cleanup order, overlay, or
  Compose implementation was changed.

## Concerns

- The prior aggregate Compose browser timeout with the date popover open remains
  a verification concern. This wave has no frontend diff and, per the brief,
  did not rerun or weaken that gate.
- The two retained P3 findings remain intentionally outside this fix scope.
- The unchanged Starlette/httpx deprecation warning remains.
