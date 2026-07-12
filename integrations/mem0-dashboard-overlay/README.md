# Mem0 Dashboard Overlay

This overlay unlocks selected self-hosted dashboard pages by replacing the
upstream Cloud-only page implementations with pages backed by
`mem0-platform-sidecar`.

Phase 1 covers:

- Categories
- Export

That means Phase 1 self-hosts those two pages only. Other Cloud-only dashboard
pages and features remain unchanged and are not implemented by this overlay.

Categories and Export appear as first-class `MEMORY TOOLS` in the dashboard
navigation. Categories starts with a form builder, with the advanced raw schema
editor available as a fallback. Category collection and individual mutations are
proxied through the configured runtime project scope. Export remains JSON-only;
CSV and Pydantic choices are visible but disabled as future formats.

Apply to an upstream dashboard checkout:

```bash
python integrations/mem0-dashboard-overlay/scripts/apply-dashboard-overlay \
  /path/to/mem0/server/dashboard
```

Verify:

```bash
python integrations/mem0-dashboard-overlay/scripts/verify-dashboard-overlay \
  /path/to/mem0/server/dashboard
```

The overlay uses a same-origin Next.js proxy route at `/api/sidecar/...`.
Configure the dashboard runtime with:

```bash
SIDECAR_INTERNAL_API_URL=http://mem0-platform-sidecar:8765
SIDECAR_PROJECT_ID=default
# Only mirror this when the Mem0 OSS server itself runs auth-disabled for local dev.
AUTH_DISABLED=false
```

The dashboard runtime, not the sidecar service, reads
`SIDECAR_INTERNAL_API_URL`. It also reads `SIDECAR_PROJECT_ID` through a
server-side runtime config route; if unset, the overlay falls back to
`MEM0_SIDECAR_DEFAULT_PROJECT_ID`, then `default`. This keeps the selected
sidecar project out of the browser build artifact.

The sidecar proxy validates the dashboard refresh-token cookie by default. If
the Mem0 OSS server is intentionally running with `AUTH_DISABLED=true` for local
development, set `AUTH_DISABLED=true` in the dashboard runtime as well so the
overlay follows that mode. Keep auth disabled off in production.

The proxy enforces the configured dashboard project for every forwarded
Categories and Export request. Caller-supplied `project_id` values in paths,
query strings, or export request bodies are rewritten to `SIDECAR_PROJECT_ID`.

If the overlay fails verification or an upstream dashboard upgrade breaks the
checkout, back it out before applying it again:

1. Run `git status` in the dashboard checkout and inspect the overlay-applied
   files.
2. Revert only the overlay changes with the dashboard checkout's VCS tools, or
   replace the checkout with a clean copy if you used a throwaway tree.
3. Avoid `git reset --hard` unless you have a backup and are intentionally
   discarding local work.
4. Rebuild and restart the dashboard if the reverted checkout is already
   deployed.
