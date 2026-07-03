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

## Run HTTP Health Route

```bash
uvicorn mem0_sidecar.http_adapter.app:create_app --factory --host 127.0.0.1 --port 8765
curl http://127.0.0.1:8765/healthz
```

Expected response:

```json
{"status":"ok","service":"mem0-platform-sidecar"}
```

## Configuration

The service reads these environment variables:

- `MEM0_SIDECAR_DATABASE_URL`
- `MEM0_SIDECAR_MEM0_BASE_URL`
- `MEM0_SIDECAR_MEM0_API_KEY`
- `MEM0_SIDECAR_DEFAULT_PROJECT_ID`
- `MEM0_SIDECAR_WORKER_POLL_INTERVAL_SECONDS`
