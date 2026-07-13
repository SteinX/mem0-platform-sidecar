# Mem0 Dashboard Overlay

This overlay replaces selected Cloud-only dashboard pages with self-hosted pages
backed by `mem0-platform-sidecar`.

The self-hosted surface currently includes:

- Categories
- Export (JSON only)
- Memory Explorer at `/dashboard/memories`
- Request Trace Explorer at `/dashboard/requests`

Entities are not part of this overlay. Categories, Export, Memory Explorer, and
Request Trace Explorer appear as first-class self-hosted dashboard surfaces.

## Apply and verify

Apply the overlay to a clean upstream dashboard checkout:

```bash
python integrations/mem0-dashboard-overlay/scripts/apply-dashboard-overlay \
  /path/to/mem0/server/dashboard
python integrations/mem0-dashboard-overlay/scripts/verify-dashboard-overlay \
  /path/to/mem0/server/dashboard
```

The apply script copies every path in `manifest.json` and overwrites matching
files. Commit or otherwise back up local dashboard changes before applying it.

Configure the dashboard runtime, not the browser build, with:

```bash
SIDECAR_INTERNAL_API_URL=http://mem0-platform-sidecar:8765
SIDECAR_PROJECT_ID=default
# Optional: pin Memory and Request Trace Explorers to one app in the project.
SIDECAR_APP_ID=default
# Mirror this only when the Mem0 OSS server itself is intentionally auth-disabled.
AUTH_DISABLED=false
```

`SIDECAR_PROJECT_ID` is authoritative for every forwarded request. If it is
unset, the overlay falls back to `MEM0_SIDECAR_DEFAULT_PROJECT_ID`, then
`default`. `SIDECAR_APP_ID`, when set, is the authoritative app for Memory
Explorer query/detail/history/mutation calls and Request Trace query/detail
calls. When it is unset, the sidecar uses the existing project's
`default_app_id`; read routes never create a missing project. Caller-supplied
project/app values are removed by the same-origin proxy.

The proxy validates the dashboard refresh-token cookie by default.
`AUTH_DISABLED=true` in the dashboard bypasses that cookie check only to mirror
an explicitly auth-disabled local Mem0 OSS runtime. It does not authenticate the
sidecar or make auth-disabled mode suitable for production.

## Memory Explorer API

The dashboard calls the same-origin `/api/sidecar` proxy. The underlying sidecar
query is `POST /v1/memories/query`:

```json
{
  "project_id": "default",
  "app_id": "default",
  "match": "all",
  "filters": [
    {"field": "category", "operator": "equals", "value": "decision"}
  ],
  "date_range": {"from": null, "to": null},
  "page": 1,
  "page_size": 20,
  "sort": "created_at_desc"
}
```

Its public envelope is:

```json
{
  "results": [],
  "page": 1,
  "page_size": 20,
  "total": 0,
  "has_more": false,
  "stale_skipped": 0
}
```

Filters support entity fields, category, memory ID, and metadata equality via
the metadata `contains` operator. Metadata filters may scan at most 5000 active
projection candidates; a larger candidate set returns HTTP 422 instead of
loading an unbounded result set. Query hydration marks missing or malformed
upstream records stale and reports how many were omitted in `stale_skipped`.

The drawer uses these scoped routes:

- `GET /v1/memories/{id}` returns the upstream memory object.
- `PATCH /v1/memories/{id}` accepts changed `text`, `metadata`, and/or
  `expiration_date`, and returns `{"memory": {...}, "event": {...}}`.
- `GET /v1/memories/{id}/history` returns `{"results": [...]}`.
- `DELETE /v1/memories/{id}` returns
  `{"memory": {...}, "event": {...}}` and removes the active projection.

Open a drawer directly with
`/dashboard/memories?memoryId=<percent-encoded-memory-id>`. Closing the drawer
removes `memoryId` while preserving the current filters and pagination.

## Request Trace Explorer API

The Requests page reads durable traces for sidecar `ADD`, `SEARCH`, and
`GET ALL` operations (`memory.add`, `memory.search`, and `memory.list`). Both
successful and failed operations are recorded. `POST /v1/events/query` accepts
the configured project/app scope plus operation, status, has-results, date,
entity, and paging filters. `GET /v1/event/{id}` supplies the selected drawer.
The same-origin proxy permits only those read routes, overwrites project/app
scope, and rejects a scoped JSON request body larger than 65,536 bytes.

Trace correlation IDs come from the configured request ID header
(`X-Request-ID` by default), so a dashboard row can be matched to sidecar HTTP
logs without exposing an upstream credential. Search/list result counts and
previews are computed only after project/app filtering. At most 20 memory
previews are persisted and returned; `result_previews_omitted` and
`result_previews_scan_truncated` disclose additional or unscanned results.

### Trace safety, retention, and backups

Trace request, response, and error documents are sanitized before persistence
and again when legacy rows are serialized. Keys are matched case-insensitively
after removing punctuation. These credential keys are redacted:

- `authorization`
- `api_key` / `apikey`
- `token` / `access_token` / `refresh_token`
- `password` / `secret`
- `cookie` / `set-cookie`
- `x-api-key`

Credential assignments embedded in strings and sidecar/upstream internal URLs
are also removed. Individual strings are bounded to 4,096 UTF-8 bytes; arrays,
nesting, and trace traversal are bounded; and each persisted request, response,
or error document is at most 65,536 bytes. Oversized values are replaced with
explicit truncation metadata. This is a trace-storage boundary, separate from
the same-origin proxy's 65,536-byte scoped request-body limit.

Sanitized traces are still internal memory data: queries, entity IDs, memory
text previews, categories, timestamps, and operational errors can remain
sensitive. Do not expose the sidecar event endpoints publicly. Apply the same
access controls, encryption, backup retention, and deletion procedures to the
sidecar database and its backups as to the memory store itself. Redaction in
traces does not remove the original content or metadata from Mem0 OSS.

There is no automatic trace-pruning job yet. Retention is deployment-owned:
operators must size and back up the database, define an acceptable retention
window, and remove old event rows through their controlled database maintenance
process until a supported pruning job is added in a later phase.

## Reconciliation and adoption

Call reconciliation directly on the sidecar; it is intentionally not exposed
by the dashboard proxy:

```bash
curl -X POST http://127.0.0.1:8765/v1/projects/default/memories/reconcile \
  -H 'Content-Type: application/json' \
  -d '{"project_id":"default","app_id":"default","adopt_unscoped":false}'
```

Records written through the sidecar carry project/app scope markers and are
safe to import into their matching projection. The response counters are
`scanned`, `indexed`, `skipped_unscoped`, `skipped_other_scope`, and
`stale_marked`. A complete scan marks projections absent upstream as stale. A
scan at the 5000-record reconciliation cap is treated as truncated and does not
mark unseen projections stale.

Unscoped adoption is disabled by default. It requires both
`adopt_unscoped=true` on the request and
`MEM0_SIDECAR_ALLOW_ADOPT_UNSCOPED=true` in the sidecar runtime, and only the
configured default project may adopt. This is a high-risk, one-project migration
decision: never enable it for shared upstream stores, because unmarked records
do not contain enough ownership evidence to distinguish tenants or projects.
Enable it for a bounded migration, reconcile and verify the counters, then turn
the runtime gate off again.

## Remove the overlay

1. Stop traffic to the dashboard or prepare a rollback deployment.
2. In the upstream dashboard checkout, inspect `git status` and restore every
   overlay path listed in `integrations/mem0-dashboard-overlay/manifest.json`
   from the exact upstream revision used before apply. Do not blindly delete
   paths: the overlay replaces some files that exist upstream.
3. Re-run the upstream dashboard typecheck/build, then rebuild and restart the
   dashboard.
4. Remove the overlay-specific runtime variables if no other deployment uses
   them. Sidecar projection data is separate and is not deleted by UI rollback.

On a disposable checkout, deleting it and recreating the pinned upstream
revision is the least ambiguous rollback. Avoid destructive VCS commands in a
checkout that contains uncommitted work.
