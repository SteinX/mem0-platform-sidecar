# Mem0 Dashboard Overlay

This overlay unlocks selected self-hosted dashboard pages by replacing the
upstream Cloud-only page implementations with pages backed by
`mem0-platform-sidecar`.

Phase 1 covers:

- Categories
- Export

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

The overlay expects dashboard runtime variable `SIDECAR_INTERNAL_API_URL` for
the Next.js sidecar proxy route.
