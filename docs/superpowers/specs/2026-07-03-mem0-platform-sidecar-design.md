# Mem0 Platform Sidecar Design

Date: 2026-07-03
Status: Draft design

## Background

Mem0 OSS is already close to Mem0 Cloud/Platform at the core memory-engine
level: memory extraction, storage, vector search, and CRUD behavior can remain
inside the upstream OSS service.

The current gaps are mostly control-plane and compatibility semantics expected
by official Mem0 integrations such as Codex, OpenCode, OpenClaw, Hermes Agent,
Pi Agent, and the hosted Mem0 MCP server:

- durable async events and event status lookup;
- project or account level category configuration;
- Platform-compatible HTTP endpoint and SDK response shapes;
- consistent `user_id`, `agent_id`, `app_id`, and `run_id` scoping;
- entity list/delete behavior;
- export/import and background job management;
- later dashboard, analytics, and webhook surfaces.

Current ecosystem research did not identify a mature open-source drop-in layer
that fills these Mem0 Platform compatibility gaps while continuing to use Mem0
OSS as the memory engine. The sidecar should fill that gap without patching
Mem0 OSS or forking official integrations deeply.

## Decision

Create a new standalone service repository named `mem0-platform-sidecar`.

The sidecar is a control-plane-first compatibility layer. It is not:

- a plugin repository;
- a `mem0-oss-mcp` v2 extraction;
- a Mem0 OSS fork;
- a full self-hosted Mem0 Cloud clone.

Mem0 OSS remains the data-plane memory engine. The sidecar owns control-plane
state and protocol compatibility, then forwards memory CRUD/search operations
to Mem0 OSS through its REST API.

The existing `mem0-oss-mcp` repository should remain the plugin, installer, and
adapter repository. Over time it can default generated plugins and installer
configuration toward the sidecar.

## Goals

1. Provide a durable control plane for Mem0 OSS deployments.
2. Offer Platform-compatible HTTP API behavior for official SDK/plugin paths.
3. Offer hosted-MCP-compatible tools for coding-agent integrations.
4. Preserve project isolation and scope semantics across shared infrastructure.
5. Keep compatibility code upgrade-safe by using Mem0 OSS REST as the data-plane
   contract instead of writing to internal OSS storage.
6. Make first-phase behavior testable with contract tests and real plugin smoke
   tests.

## Non-Goals

The first phase will not implement:

- a web dashboard;
- analytics UI;
- organization, team, billing, or permission management;
- webhook delivery UI;
- replacement vector search or memory extraction;
- direct patches to upstream Mem0 OSS;
- direct writes to Mem0 OSS internal database tables;
- full parity with every hosted Mem0 Platform product surface.

## High-Level Architecture

```text
Agents / plugins / SDK clients / MCP clients
  -> sidecar HTTP API + sidecar MCP server
     -> sidecar DB: projects, api_keys, categories, memories_index,
                    events, entities, jobs
     -> Mem0 OSS REST API: memory add/search/get/update/delete
```

The sidecar is the client-facing memory API. Mem0 OSS is the memory engine.

Every mutating sidecar operation should create a durable event record even when
the underlying Mem0 OSS call completes synchronously. Background work should be
tracked as jobs and surfaced through event status where relevant.

## Data Model

The first implementation should use SQLite, with schema and repository
boundaries designed so Postgres can be added later.

### projects

Represents a sidecar project or app scope.

Important fields:

- `id`
- `name`
- `default_user_id`
- `default_app_id`
- `default_agent_id`
- `mem0_base_url`
- `mem0_api_key_ref`
- `settings_json`
- `created_at`
- `updated_at`

The project object is the natural owner for category taxonomy, default scoping,
and adapter behavior.

### api_keys

Maps external Platform-style credentials to sidecar projects.

Important fields:

- `id`
- `key_hash`
- `prefix`
- `project_id`
- `name`
- `scopes_json`
- `created_at`
- `last_used_at`
- `revoked_at`

This lets official integrations continue using hosted-style authentication
without exposing upstream Mem0 OSS credentials directly.

### categories

Stores project-level custom categories.

Important fields:

- `id`
- `project_id`
- `name`
- `description`
- `schema_json`
- `enabled`
- `strategy`
- `version`
- `created_at`
- `updated_at`

The sidecar should accept both `custom_categories` and `customCategories`
request/response shapes where integration compatibility requires it.

### memories_index

Stores sidecar-owned projections for memories whose source of truth remains in
Mem0 OSS.

Important fields:

- `id`
- `project_id`
- `mem0_memory_id`
- `user_id`
- `agent_id`
- `app_id`
- `run_id`
- `category`
- `entity_refs_json`
- `metadata_projection_json`
- `created_at`
- `updated_at`
- `deleted_at`

The index should avoid storing full memory text as its primary purpose. It
exists to provide filtering, category, entity, export, and management behavior
that Mem0 OSS may not expose natively.

### events

Durable external operation log.

Important fields:

- `id`
- `project_id`
- `operation`
- `status`
- `subject_type`
- `subject_id`
- `request_json`
- `response_json`
- `error_json`
- `created_at`
- `started_at`
- `completed_at`

Initial statuses:

- `PENDING`
- `RUNNING`
- `SUCCEEDED`
- `FAILED`
- `CANCELLED`

Events are the compatibility surface for `list_events`, `get_event_status`,
`GET /v1/events`, and `GET /v1/event/{event_id}`.

### entities

Derived entity index for users, agents, apps, runs, and later custom entity
types.

Important fields:

- `id`
- `project_id`
- `entity_type`
- `entity_id`
- `display_name`
- `metadata_json`
- `memory_count`
- `last_seen_at`
- `created_at`
- `updated_at`

Entity data can be derived from memory operations and rebuilt from
`memories_index`.

### jobs

Internal background work queue.

Important fields:

- `id`
- `project_id`
- `event_id`
- `job_type`
- `status`
- `payload_json`
- `attempt_count`
- `max_attempts`
- `run_after`
- `locked_at`
- `completed_at`
- `error_json`
- `created_at`
- `updated_at`

Jobs are internal execution units. Events are the external compatibility and
status surface.

## Module Boundaries

```text
src/mem0_sidecar/
  core/
  store/
  mem0_client/
  http_adapter/
  mcp_adapter/
  workers/
  compat/
  config/
```

### core

Domain models and service logic for projects, categories, scopes, events,
entities, and jobs.

### store

Database repository layer. SQLite is first, but interfaces should not assume
SQLite-specific behavior where a future Postgres path is practical.

### mem0_client

The only module that talks to Mem0 OSS REST. This keeps upstream compatibility
concerns isolated.

### http_adapter

FastAPI routes that expose Platform-compatible HTTP endpoints.

### mcp_adapter

MCP tool listing and tool call handlers. MCP handlers should call core services,
not HTTP route functions.

### workers

Background job runner for export, import, category backfill, entity rebuild,
webhook delivery, and later maintenance tasks.

### compat

Request/response shape translation for Codex, OpenCode, OpenClaw, Hermes Agent,
Pi Agent, hosted MCP, and Platform SDK expectations.

### config

Environment, config files, project bootstrap, API-key loading, and Mem0 OSS
target configuration.

## Technology Stack

- Python service.
- FastAPI for HTTP endpoints.
- SQLAlchemy and Alembic for database models and migrations.
- SQLite-first local storage, with a future Postgres path.
- httpx for Mem0 OSS REST calls.
- Built-in async worker loop for first-phase background jobs.
- Docker Compose example for local Mem0 OSS plus sidecar.
- pytest for unit, integration, contract, and end-to-end tests.

Avoid Celery, Redis, Kafka, or a large dashboard framework in the first phase.
Those can be added when the sidecar has stable control-plane semantics.

## First-Phase HTTP Compatibility Scope

Implement enough Platform-compatible HTTP behavior for official integration
critical paths:

- `POST /v3/memories/add`
- `POST /v3/memories/search`
- `GET /v3/memories`
- `POST /v3/memories`
- `GET /v3/memories/{id}`
- `PUT /v3/memories/{id}`
- `PATCH /v3/memories/{id}`
- `DELETE /v3/memories/{id}`
- `DELETE /v3/memories`
- `GET /v1/events`
- `GET /v1/event/{event_id}`
- project config get/update endpoints for custom categories

The exact project endpoint shape should be chosen during implementation after
checking current official SDK behavior. The sidecar should preserve both snake
case and camel case category field variants where needed.

## First-Phase MCP Compatibility Scope

Expose the hosted Mem0 MCP tool family:

- `add_memory`
- `search_memories`
- `get_memory`
- `get_memories`
- `update_memory`
- `delete_memory`
- `delete_all_memories`
- `list_entities`
- `delete_entities`
- `list_events`
- `get_event_status`

The MCP adapter should preserve `user_id`, `agent_id`, `app_id`, and `run_id`
as first-class scope inputs and store any compatibility projection needed by
the sidecar.

## First-Phase Integration Validation

Use official integrations as compatibility samples:

- Codex: verify official plugin skills and hooks can read, write, categorize,
  and inspect events.
- OpenCode: verify SDK-style project/category/event/status paths.
- OpenClaw: verify Platform-mode event and entity tools.
- Hermes Agent: verify it can continue direct OSS mode and can also use sidecar
  where Platform parity matters.
- Pi Agent: use as a reference for category and `customCategories` response
  shapes, but do not make it a first-phase required smoke test.

## Phased Roadmap

### Phase 0: Design and Repo Bootstrap

Deliver:

- design documentation;
- repository skeleton;
- configuration format;
- local Docker Compose development environment;
- basic CI;
- lint/test conventions.

### Phase 1: Control Plane Core

Deliver:

- project, API key, category, memory index, event, entity, and job models;
- migrations;
- repository layer;
- core service logic;
- durable event creation for mutating operations;
- category projection and filtering logic;
- basic worker loop.

### Phase 2: HTTP Platform Compatibility

Deliver:

- first-phase `/v3/memories/*` endpoints;
- `/v1/events` and `/v1/event/{event_id}` endpoints;
- project/custom category configuration endpoints;
- contract tests for response shapes and error behavior.

### Phase 3: MCP Compatibility

Deliver:

- hosted-MCP-compatible tool list;
- tool call handlers backed by core services;
- event and entity tool behavior;
- contract tests for tool inputs and outputs.

### Phase 4: Plugin End-to-End Validation

Deliver:

- repeatable Codex smoke test;
- repeatable OpenCode smoke test;
- repeatable OpenClaw smoke test;
- Hermes direct-OSS and sidecar compatibility notes;
- documented configuration examples for each integration.

### Phase 5: Operational Features

Deliver:

- export jobs;
- import jobs;
- category backfill;
- entity rebuild;
- job retry and cancellation;
- basic webhook registration and delivery attempts.

Dashboard and analytics remain later product work.

## First-Version Acceptance Criteria

1. The sidecar can connect to an existing Mem0 OSS REST service.
2. Memory add/search/get/update/delete through the sidecar persists data in
   Mem0 OSS.
3. Each mutating operation creates a durable event that can be listed and
   queried by status.
4. Project-level custom categories can be configured and returned through
   Platform-compatible response shapes.
5. Category projection participates in add/search/list behavior.
6. MCP tools and HTTP endpoints both have contract tests.
7. At least one official plugin path, preferably Codex or OpenClaw, passes a
   repeatable smoke test.
8. The sidecar does not patch Mem0 OSS and does not depend on internal Mem0 OSS
   database schema.

## Open Questions

1. Should categories be scoped by API key/account, by `app_id`, or by both?
2. Should the sidecar issue hosted-style keys such as `m0-*`, or keep a simpler
   local bearer-token model with API-key mapping?
3. Should category classification start as deterministic metadata/rule mapping,
   or use an optional LLM classifier from the Mem0 OSS configuration?
4. Should Hermes documentation recommend direct OSS mode by default, with sidecar
   mode only when Platform parity is required?
5. How much full memory text, if any, should `memories_index` retain for export
   and dashboard workflows?

## Implementation Notes

- Keep control-plane services independent from HTTP route functions.
- Keep MCP handlers independent from HTTP route functions.
- Treat Mem0 OSS REST as the only data-plane contract.
- Prefer overfetch plus sidecar post-filtering for filters OSS cannot push down
  cleanly.
- Preserve `app_id` as a first-class project scope to avoid collapsing unrelated
  workspaces into a single namespace.
- Make destructive operations scope-explicit and evented.
- Keep official plugin overlays thin and upgrade-safe.

