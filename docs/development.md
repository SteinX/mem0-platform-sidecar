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
