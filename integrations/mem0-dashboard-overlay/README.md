# Mem0 Dashboard Overlay

This overlay unlocks selected self-hosted dashboard pages by replacing the
upstream Cloud-only page implementations with pages backed by
`mem0-platform-sidecar`.

Phase 1 covers:

- Categories
- Export

That means Phase 1 self-hosts those two pages only. Other Cloud-only dashboard
pages and features remain unchanged and are not implemented by this overlay.

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
NEXT_PUBLIC_MEM0_SIDECAR_PROJECT_ID=default
```

The dashboard runtime, not the sidecar service, reads
`SIDECAR_INTERNAL_API_URL`. The browser bundle reads
`NEXT_PUBLIC_MEM0_SIDECAR_PROJECT_ID`; set it to the same project used by the
sidecar deployment, especially when `MEM0_SIDECAR_DEFAULT_PROJECT_ID` is not
`default`.

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
