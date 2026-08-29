# Transparent Ingress Compatibility Contract v1

This document defines the public memory protocol implemented by
`mem0-platform-sidecar` when the deployment enables transparent ingress.
Clients use their normal Mem0 base URL and credential. They do not configure or
discover a sidecar-specific URL.

## Scope and identity

Every request resolves a project and app before reaching `MemoryService`.
For trusted `admin` and internal `system` principals, resolution order is:

1. `X-Mem0-Project-ID` and `X-Mem0-App-ID` headers;
2. top-level `project_id` and `app_id`;
3. `filters.project_id` and `filters.app_id`;
4. sidecar scope metadata already attached to a projected memory;
5. the configured default project and that project's default app.

The three standard Mem0 entity identifiers remain `user_id`, `agent_id`, and
`run_id`. At least one is required by `POST /memories`. An unscoped
`GET /memories` is admin-only.

The optional project/app selectors are operator controls. Ordinary authenticated
members cannot supply project or app overrides through headers, body, query,
filters, or sidecar metadata; their requests use the configured default project
and that project's default app. This prevents a valid Core credential from
becoming a cross-project sidecar credential. The ingress strips client-supplied
scope headers and supplies only server-owned defaults. The same boundary applies
to the Platform-shaped `/v3/memories/*` and `/v1/memories/*` routes:
`project_wide` and memory reconciliation require an `admin` or `system`
principal.

## Authentication

The sidecar accepts the same client credential headers as Core:

- `Authorization: Bearer <JWT>`
- `X-API-Key: <user-or-admin-key>`

The credential is checked through Core's `/auth/me` endpoint. Authentication
headers are never written to sidecar Events, application logs, errors, or
responses. Authentication transport failures return `503`; invalid credentials
preserve Core's `401` or `403` status and `detail` envelope.

Inbound authentication is disabled by default in the image so the feature can
be deployed before ingress cutover. The deployment enables it atomically with
the ingress and internal-caller credential changes.

## Routes

### `POST /memories`

Request JSON:

```json
{
  "messages": [{"role": "user", "content": "Prefers tea"}],
  "user_id": "u1",
  "agent_id": null,
  "run_id": null,
  "app_id": "repo",
  "metadata": {"type": "preference"},
  "expiration_date": "2027-01-01",
  "infer": true,
  "memory_type": null,
  "prompt": null
}
```

`messages` is required. Each item contains string `role` and `content`.
`metadata`, `expiration_date`, `infer`, `memory_type`, and `prompt` are
optional and retain Core semantics. Success is `200` with the unmodified Core
add result, normally:

```json
{"results": [{"id": "memory-id", "memory": "Prefers tea", "event": "ADD"}]}
```

Sidecar-only Event and mutation-intent representations are not embedded in this
response.

### `GET /memories`

Query parameters:

- `user_id`, `agent_id`, `run_id`: optional entity filters;
- `app_id`, `project_id`: optional transparent-ingress scope;
- `top_k`: integer from 0 through the configured Core list limit;
- `show_expired`: boolean, default false.

Pinned Core `v2.0.19-steinx.1` returns an object for a scoped list, so
success is `200` with:

```json
{"results": [{"id": "memory-id", "memory": "Prefers tea"}]}
```

The sidecar preserves the upstream object and its stable keys instead of
synthesizing a different envelope. Legacy array responses remain arrays.

When no entity identifier is supplied, a non-admin principal receives `403`.

### `POST /search`

Request JSON:

```json
{
  "query": "tea",
  "filters": {"user_id": "u1", "app_id": "repo"},
  "top_k": 10,
  "threshold": 0.2,
  "explain": false,
  "show_expired": false
}
```

Top-level `user_id`, `agent_id`, and `run_id` remain accepted for compatibility
and override the same keys inside `filters`, matching Core. `filters: null` is
accepted as an empty filter object. Pinned Core returns a top-level array for a
scoped search, so success is `200` with:

```json
[{"id": "memory-id", "memory": "Prefers tea", "score": 0.91}]
```

### `GET /memories/{memory_id}`

Success is `200` with the Core memory object. A missing projected or upstream
memory returns `404`.

### `PUT /memories/{memory_id}`

Request JSON may contain any explicitly supplied subset of:

```json
{"text": "Prefers green tea", "metadata": {}, "expiration_date": null}
```

An omitted field is unchanged. An explicitly null `expiration_date` clears
expiration. Success preserves the Core update response. The sidecar refreshes
its projection before responding.

### `GET /memories/{memory_id}/history`

Success is `200` with the unmodified Core history result. Pinned Core returns a
top-level array. The route uses the same opaque and single-decode memory-ID
rules as Platform REST.

### `DELETE /memories/{memory_id}`

Success is:

```json
{"message": "Memory deleted successfully"}
```

Deletion uses the sidecar mutation-intent path and records one canonical client
Event.

### `DELETE /memories`

At least one of `user_id`, `agent_id`, or `run_id` is required. The caller must
be an admin. Success is:

```json
{"message": "All relevant memories deleted"}
```

The sidecar processes bounded target batches and resumes an active partial
operation without repeating completed targets.

## Error envelope

Public validation and application errors use FastAPI's stable envelope:

```json
{"detail": "human-readable message"}
```

The compatibility profile uses:

- `400` for Core-compatible entity/filter validation;
- `401` for missing or invalid authentication;
- `403` for valid non-admin callers performing admin operations;
- `404` for missing memories;
- `409` for retryable mutation conflicts;
- `422` for request schema or sidecar scope validation;
- `503` when inbound authentication cannot reach Core.

## Observability

Each accepted memory operation, including a projected-memory `404`, creates one
sidecar Event and preserves or generates `X-Request-ID`. The sidecar Event is
the canonical public request record. Core request logs describe internal
data-plane calls after cutover. Direct-write reconciliation never synthesizes a
client Event.

## Explicit exclusions

`POST /reset`, raw database access, future unknown Core routes, and historical
hosted-only Mem0 endpoints are not part of this profile.
