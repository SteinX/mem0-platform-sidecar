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
Mem0 OSS-compatible REST service over HTTP.

Required:

- `MEM0_E2E_BASE_URL`

Optional:

- `MEM0_E2E_API_KEY`
- `MEM0_E2E_PROJECT_ID`

Example:

```bash
MEM0_E2E_BASE_URL=http://127.0.0.1:8000 \
MEM0_E2E_PROJECT_ID=sidecar-e2e \
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/e2e/test_live_mem0_oss.py -v -rs -p no:cacheprovider
```

When `MEM0_E2E_BASE_URL` is not set, the live E2E test skips instead of
failing. The live test creates a marker memory, searches it, reads it, deletes
it, and asserts durable sidecar add/delete events.

## Suggested Verification

Run the default regression suite:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
```

This command allows the live Mem0 OSS E2E test to skip when
`MEM0_E2E_BASE_URL` is not set. A skipped live E2E test is not a live pass.

Run the real live check explicitly when a compatible service is available:

```bash
MEM0_E2E_BASE_URL=http://127.0.0.1:8000 \
MEM0_E2E_PROJECT_ID=sidecar-e2e \
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/e2e/test_live_mem0_oss.py -v -rs -p no:cacheprovider
```
