# E2E Testing

The sidecar has mock-upstream tests and an isolated live Mem0 OSS harness.

## Mock-upstream E2E

This exercises the FastAPI app, SQLite projection, `MemoryService`, and Mem0
client boundary without Docker:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  tests/http_adapter/test_memory_routes.py -v -p no:cacheprovider
```

## Live Mem0 OSS E2E

The live harness starts a unique Compose project containing Mem0 OSS,
Postgres/pgvector, and an OpenAI-compatible stub. Tests run inside that isolated
network and never use a host-published port or shared Compose volume. The runner
always executes `docker compose down -v --remove-orphans --rmi local` in a
`finally` path; test failures therefore still trigger cleanup.

The default runner proves the real lifecycle
`add -> query -> detail -> patch -> history -> delete`. The lifecycle creates
unique project, app, user, agent, run, category, and marker IDs; every other
fixture also uses unique identifiers for the scopes it exercises. The test
queries with entity, category, and date filters, patches
text/metadata/expiration, polls history with a deadline and last-response
diagnostic, verifies wrong-app query and detail isolation, and deletes through
the scoped sidecar route.
The deleted record no longer appears in active projection/query results; its
deleted_at tombstone remains for audit and
stale-index bookkeeping. Query responses also assert the `stale_skipped` count.

The same live lifecycle also proves durable request traces against the real
Mem0 OSS service: a correlated `ADD`, `SEARCH`, and `GET ALL` are queried through
`POST /v1/events/query`; the search drawer payload is fetched through
`GET /v1/event/{id}`; result counts and previews are checked after app-scope
filtering; and a post-delete `GET ALL` proves the no-results trace. A synthetic
nested credential and internal Mem0 URL are submitted with the fixture, then
the raw sidecar `events.request_json`, `response_json`, and `error_json` columns
are checked to ensure neither value was persisted and every document remains
within 65,536 bytes. Fixture deletion runs before those trace assertions, so a
trace regression does not strand the upstream memory.

Reconciliation coverage imports records bearing sidecar project/app markers and
checks the `scanned`, `indexed`, `skipped_unscoped`, `skipped_other_scope`, and
`stale_marked` counters. The default service verifies that
`adopt_unscoped=true` is rejected while
`MEM0_SIDECAR_ALLOW_ADOPT_UNSCOPED` is absent. A separate adoption-only runner
is the sole service with that gate enabled and an explicit default project; it
adopts one uniquely identified unscoped fixture, deletes it through its scoped
route, and checks both that no active projection remains and that the upstream
list no longer contains its ID. All fixture cleanup falls back to a direct
upstream delete when scoped cleanup fails, then proves absence from the upstream
list before the isolated stack is removed.

Unscoped adoption is a high-risk, one-project migration decision. Never enable
the gate in shared upstream stores: unmarked data contains no reliable project
ownership evidence. The dedicated test service is an isolation mechanism, not
a production configuration example.

Run the harness:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/run_live_e2e_compose.py
```

Optional controls:

- `MEM0_E2E_PROJECT_ID` overrides the generated unique test project.
- `MEM0_E2E_COMPOSE_PROJECT` overrides the generated unique Compose project.
- `MEM0_E2E_STARTUP_TIMEOUT` sets the health polling deadline in seconds.
- `MEM0_E2E_UPSTREAM_CONTEXT` selects the local Mem0 OSS build context.

The harness polls Mem0 health until the deadline; it does not use a fixed startup
sleep. On timeout or test failure it prints Compose status and bounded logs for
the Mem0, Postgres, stub, and runner services before cleanup. The isolated Mem0
service sets its list limit to 5000 so the reconciliation contract can request
the sidecar's bounded scan size.

For manual debugging against an already running compatible backend, set
`MEM0_E2E_BASE_URL` and run the test directly. Without it, live tests skip; a
skip is not a live pass. The adoption test additionally skips unless it runs in
the dedicated adoption service.

```bash
MEM0_E2E_BASE_URL=http://127.0.0.1:8000 \
MEM0_E2E_PROJECT_ID=sidecar-manual-e2e \
PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  tests/e2e/test_live_mem0_oss.py -v -rs -m 'not adoption_e2e' \
  -p no:cacheprovider
```

Do not place API keys in this document or Compose file. If a manual backend
requires one, provide `MEM0_E2E_API_KEY` only in the invoking process environment.

## Request trace regression coverage

The non-Docker integration test uses the complete FastAPI, SQLAlchemy, and
SQLite path with a deterministic in-process Mem0 client. It covers successful
and failed search, list results and no-results, add correlation, operation,
status, date and page filters, app/project isolation, the public detail shape,
20-preview capping and omission counts, nested credential keys and credential
assignments in string values, internal URL removal, a 70 KiB payload, malformed
legacy JSON, and direct raw-event JSON inspection.

Run it together with the live module's non-live helpers (live cases skip unless
`MEM0_E2E_BASE_URL` is configured):

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  tests/integration/test_request_trace_flow.py \
  tests/e2e/test_live_mem0_oss.py -q -p no:cacheprovider
```

Trace redaction protects the event store, not the source memory. The real
memory text/metadata remains internal sensitive data in Mem0 OSS. Sidecar event
rows and all database backups must share the memory store's access and backup
controls. Until a supported pruning job exists, trace retention and old-row
cleanup are owned by the deployment operator.

## Acceptance commands

Run the normal suite (live tests may skip):

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
python -m ruff check .
```

Then run the live command above. Success requires both runner services to pass,
the final cleanup command to succeed, and no resources bearing that unique
Compose project name to remain.
