# E2E Testing

The sidecar has two E2E levels.

## Mock-Upstream E2E

This runs the FastAPI app, SQLite sidecar database, `MemoryService`, and the
Mem0 client boundary against a fake upstream client.

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/http_adapter/test_memory_routes.py -v -p no:cacheprovider
```

## Live Mem0 OSS E2E

This runs the FastAPI sidecar app in-process while the sidecar calls a real
Mem0 OSS REST service over HTTP. The default harness starts a temporary local
compose stack with Mem0 OSS, Postgres/pgvector, and an OpenAI-compatible stub,
runs the live test inside the compose network, and then removes the containers
and volumes.

Optional:

- `MEM0_E2E_PROJECT_ID`
- `MEM0_E2E_COMPOSE_PROJECT`
- `MEM0_E2E_STARTUP_TIMEOUT`

Example:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/run_live_e2e_compose.py
```

The compose harness does not rely on a host-published port. It points the test
at `http://mem0:8000` inside the compose network, so it also works in local
Docker environments where published ports are not reachable from the shell.
The live test creates a marker memory, searches it, reads it, deletes it, and
asserts durable sidecar add/delete events.

For manual debugging against an already running compatible backend, set
`MEM0_E2E_BASE_URL` and run the pytest file directly. When
`MEM0_E2E_BASE_URL` is not set, the direct pytest invocation skips instead of
failing.

## Suggested Verification

Run the default regression suite:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
```

This command allows the live Mem0 OSS E2E test to skip when
`MEM0_E2E_BASE_URL` is not set. A skipped live E2E test is not a live pass.

Run the real live check through the local compose harness:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/run_live_e2e_compose.py
```

Use the direct pytest path only for manual backend debugging:

```bash
MEM0_E2E_BASE_URL=http://127.0.0.1:8000 \
MEM0_E2E_PROJECT_ID=sidecar-e2e \
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/e2e/test_live_mem0_oss.py -v -rs -p no:cacheprovider
```
