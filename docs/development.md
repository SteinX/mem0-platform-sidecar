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

The dev compose file runs the sidecar only. By default it expects a Mem0
OSS-compatible REST service on the host at `http://127.0.0.1:8000`, reached from
the container through `host.docker.internal`. The image runs
`python -m alembic upgrade head` before starting Uvicorn.

```bash
docker compose -f docker/docker-compose.dev.yml up --build
```

Override `MEM0_SIDECAR_MEM0_BASE_URL` when Mem0 is on another Docker network,
for example `MEM0_SIDECAR_MEM0_BASE_URL=http://mem0:8000`.

## Configuration

The service reads these environment variables:

- `MEM0_SIDECAR_DATABASE_URL`
- `MEM0_SIDECAR_MEM0_BASE_URL`
- `MEM0_SIDECAR_MEM0_API_KEY`
- `MEM0_SIDECAR_DEFAULT_PROJECT_ID`
- `MEM0_SIDECAR_WORKER_POLL_INTERVAL_SECONDS`
