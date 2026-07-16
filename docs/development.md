# Development

## Install

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Test

```bash
python -m pytest -v
```

## E2E

See [E2E Testing](e2e.md).

## Run HTTP Routes

```bash
uvicorn mem0_sidecar.http_adapter.app:create_app --factory --host 127.0.0.1 --port 8765
curl http://127.0.0.1:8765/healthz
curl http://127.0.0.1:8765/readyz
```

`/healthz` is a liveness check only. Expected response:

```json
{"status":"ok","service":"mem0-platform-sidecar"}
```

`/readyz` checks that the sidecar database session can execute a simple query.
It does not prove live Mem0 OSS read/write behavior; use the live E2E command
for that.

## Docker Dev Compose

The dev compose file runs the sidecar only. It does not assume where Mem0 OSS
runs; set `MEM0_SIDECAR_MEM0_BASE_URL` to a REST base URL that is reachable from
the sidecar container. The image runs `python -m alembic upgrade head` before
starting Uvicorn.

```bash
cp .env.example .env
# edit MEM0_SIDECAR_MEM0_BASE_URL for your Docker network or gateway
docker compose -f docker/docker-compose.dev.yml up --build
```

Common `MEM0_SIDECAR_MEM0_BASE_URL` shapes:

- Same compose network: `http://mem0:8000`
- External Docker network alias: `http://mem0-api:8000`
- Gateway with path prefix: `https://gateway.example/mem0`
- Host service on Docker Desktop: `http://host.docker.internal:8000`

On Linux, `host.docker.internal` may require an explicit Compose `extra_hosts`
entry in your deployment file. Prefer a Docker network alias when possible.

## Dashboard Overlay

Apply the overlay to an upstream dashboard checkout, then verify it before you
wire the runtime variable:

```bash
python integrations/mem0-dashboard-overlay/scripts/apply-dashboard-overlay \
  /path/to/mem0/server/dashboard
python integrations/mem0-dashboard-overlay/scripts/verify-dashboard-overlay \
  /path/to/mem0/server/dashboard
```

The dashboard runtime, not the sidecar service, reads
`SIDECAR_INTERNAL_API_URL`. For local or Docker-based dashboard runs, point it
at the sidecar service URL, for example
`SIDECAR_INTERNAL_API_URL=http://mem0-platform-sidecar:8765`.

Phase 1 only self-hosts Categories and Export. Other Cloud-only dashboard
pages and features remain unchanged and are not implemented by this overlay.

If verification fails or an upstream upgrade breaks the dashboard checkout,
back out the overlay before retrying:

1. Run `git status` in the dashboard checkout and review the files touched by
   the overlay.
2. Revert only the overlay-applied files with that checkout's VCS tools, or use
   a clean temp copy of the upstream dashboard if you need to start over.
3. Avoid destructive history rewrites such as `git reset --hard` unless you have
   a backup and have confirmed you are discarding only the overlay work.
4. Rebuild and restart the dashboard if it has already been deployed.

The dev compose file remains sidecar-only. It does not start the dashboard and
does not need dashboard overlay wiring. Keep `docs/superpowers/` internal and
ignored.

## Configuration

The service reads these environment variables:

- `MEM0_SIDECAR_DATABASE_URL`
- `MEM0_SIDECAR_MEM0_BASE_URL`
- `MEM0_SIDECAR_MEM0_API_KEY`
- `MEM0_SIDECAR_MEM0_API_KEY_HEADER_NAME`
- `MEM0_SIDECAR_MEM0_API_KEY_PREFIX`
- `MEM0_SIDECAR_MEM0_EXTRA_HEADERS`
- `MEM0_SIDECAR_MEM0_MEMORIES_PATH`
- `MEM0_SIDECAR_MEM0_SEARCH_PATH`
- `MEM0_SIDECAR_MEM0_REQUEST_TIMEOUT_SECONDS`
- `MEM0_SIDECAR_MEM0_CONNECT_TIMEOUT_SECONDS`
- `MEM0_SIDECAR_MEM0_VERIFY_TLS`
- `MEM0_SIDECAR_MEM0_CA_BUNDLE`
- `MEM0_SIDECAR_DEFAULT_PROJECT_ID`
- `MEM0_SIDECAR_WORKER_POLL_INTERVAL_SECONDS`
- `MEM0_SIDECAR_LOG_LEVEL`
- `MEM0_SIDECAR_LOG_FORMAT`
- `MEM0_SIDECAR_REQUEST_ID_HEADER`

For auth, the sidecar sends `MEM0_SIDECAR_MEM0_API_KEY` in
`MEM0_SIDECAR_MEM0_API_KEY_HEADER_NAME`. Set
`MEM0_SIDECAR_MEM0_API_KEY_PREFIX=Bearer` and
`MEM0_SIDECAR_MEM0_API_KEY_HEADER_NAME=Authorization` when a gateway expects
`Authorization: Bearer <token>`.

Set `MEM0_SIDECAR_MEM0_EXTRA_HEADERS` to a JSON object for gateway-specific
headers, for example `{"X-Mem0-Org":"org-1"}`.

Set `MEM0_SIDECAR_LOG_FORMAT=json` for container logs. Each request receives or
propagates the configured request ID header and emits structured request logs;
Mem0 OSS upstream calls also emit structured success/failure logs.

## Resolve an ambiguous mutation intent

An interrupted upstream mutation can remain `UNKNOWN` or become `EXHAUSTED`.
Those states intentionally block later writes in the same project/app scope:
the sidecar will not replay an add whose upstream outcome is ambiguous.

The recovery command is available only as a host/container management CLI. It
has no HTTP or dashboard route. Shell access to the sidecar container and its
database is the authentication boundary, so run it only from a trusted
operator session.

First list blockers for one exact project and app:

```bash
docker compose exec sidecar mem0-sidecar-admin mutation-intents list \
  --project-id demo-project \
  --app-id codex
```

After investigating the upstream system, copy the current intent ID, status,
and attempt count from that output. If the upstream outcome still cannot be
known, explicitly terminalize that one intent:

```bash
docker compose exec sidecar mem0-sidecar-admin mutation-intents resolve \
  --project-id demo-project \
  --app-id codex \
  --intent-id 11111111-1111-1111-1111-111111111111 \
  --confirm-intent-id 11111111-1111-1111-1111-111111111111 \
  --expected-status EXHAUSTED \
  --expected-attempt-count 3 \
  --reason "operator accepted the unknowable upstream outcome" \
  --accept-unknown-outcome
```

Resolution uses compare-and-resolve under the project mutation lock. It is
rejected if scope, status, attempt count, confirmation, or lease state changed.
For an add intent the CLI performs one read-only marker observation and refuses
resolution if the marker is present. Marker absence is not proof that the add
did not happen; `--accept-unknown-outcome` is still required.

The operation never sends an upstream write. It marks the original intent and
event failed, retains the original operation key so that the old
`Idempotency-Key` remains consumed, and records a sanitized `mutation.resolve`
audit event. Use a new idempotency key for a new request after resolution.
