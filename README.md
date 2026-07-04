# Mem0 Platform Sidecar

Control-plane sidecar for Mem0 OSS Platform compatibility.

This service owns projects, API keys, categories, memory index projections,
events, entities, and jobs. Mem0 OSS remains the memory data plane and is
accessed through REST APIs.

For Docker deployment, copy `.env.example` and set
`MEM0_SIDECAR_MEM0_BASE_URL` to the Mem0 OSS REST base URL reachable from the
sidecar container. The sidecar also supports configurable auth headers,
additional upstream headers, TLS/timeout settings, and JSON request/upstream
logs through environment variables.
