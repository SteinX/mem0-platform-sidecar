# Server-Side Memory Consolidation

Consolidation is a sidecar control-plane workflow over Mem0 OSS. Mem0 remains
the source of truth for memory text and vectors; the sidecar stores scoped
fingerprints, policy, proposals, checkpoints, and lineage.

## Safety model

- Default state is `OFF`: `MEM0_SIDECAR_CONSOLIDATION_ENABLED=false`.
- Every scan is bounded by dirty anchors and proposal limits.
- Pinned projections and `[PINNED]` records are never proposed or changed.
- Exact duplicates and configured expired ephemeral types are the only
  auto-safe actions.
- Near duplicates and contradictions are always `REVIEW_REQUIRED`.
- Apply requires unchanged source hashes and a complete JSON export checkpoint.
- Historical/direct-write projections must complete an explicit, audited
  scope-marker backfill before they are eligible for consolidation. Status
  reports the remaining count per app scope.
- Shadowing is reversible and excluded from search/list, while exact detail
  reads remain available for rollback.
- Shadow export attempts use an ownership lease, so concurrent or recovered
  workers cannot overwrite each other's terminal state.
- Hard delete defaults off, waits at least seven days, deletes one ID at a
  time, verifies semantic not-found, then records lineage. The REST adapter
  treats both HTTP 404 and the legacy Mem0 OSS `200 null` response as
  not-found.
- Finalize rechecks the bridge heartbeat plus source and canonical
  scope/hash/pin state. This includes a newly created semantic replacement,
  so originals remain recoverable if that replacement changes or disappears.
- A current bridge heartbeat must assert that both reads and writes route
  through the sidecar before `AUTO_SAFE` can enqueue work.

## Rollout states

| State | Service | Policy | Hard delete | Intended use |
| --- | --- | --- | --- | --- |
| `OFF` | disabled | disabled | disabled | deploy schema/code without behavior change |
| `OBSERVE` | enabled | `OBSERVE` | disabled | measure proposal precision and queue latency |
| `MANUAL` | enabled | `MANUAL` | disabled | operator approval, shadow, and rollback drills |
| `AUTO_SAFE` | enabled | `AUTO_SAFE` | disabled initially | automatic export and shadow for allow-listed actions |

Enable hard delete separately only after the restore gate below.

## Production migration order

1. Back up the sidecar database and export every app scope.
2. Deploy schema and code with consolidation `OFF`.
3. Deploy MCP bridge routing and OpenCode capture suppression.
4. Run direct-write sync once and require `truncated=false`.
5. For every app scope, run `scope-backfill` in bounded batches until both its
   `remaining` result and status `scope_marker_backfill_required` are zero.
   Investigate every `skipped_conflict` or `missing` result; never force a
   conflicting marker into another scope.
6. Enable `OBSERVE` for seven days; inspect proposal precision and queue latency.
7. Enable `MANUAL`; shadow exact duplicates only and exercise rollback.
8. Enable `AUTO_SAFE` with hard delete still false for fourteen days.
9. Enable hard delete only after zero incorrect-shadow incidents and a
   successful restore drill.

Initial deployment values:

```env
MEM0_SIDECAR_CONSOLIDATION_ENABLED=false
MEM0_SIDECAR_CONSOLIDATION_HARD_DELETE_ENABLED=false
MEM0_SIDECAR_CONSOLIDATION_SCHEDULER_INTERVAL_SECONDS=300
MEM0_SIDECAR_CONSOLIDATION_JOB_LEASE_SECONDS=300
MEM0_SIDECAR_CONSOLIDATION_BRIDGE_ROUTING_REQUIRED=true
```

## Operator commands

All mutating commands require exact project/app scope. Policy changes also
require `--confirm-app-id`; approvals require expected status and source hashes;
finalization requires `--confirm-hard-delete`.

```bash
mem0-sidecar-admin consolidation policy get \
  --project-id PROJECT --app-id APP

mem0-sidecar-admin consolidation policy set \
  --project-id PROJECT --app-id APP --confirm-app-id APP \
  --policy-json '{"enabled":true,"mode":"OBSERVE"}'

mem0-sidecar-admin consolidation run --dry-run \
  --project-id PROJECT --app-id APP

mem0-sidecar-admin consolidation scope-backfill \
  --project-id PROJECT --app-id APP --confirm-app-id APP \
  --confirm-writes-paused --limit 200

mem0-sidecar-admin consolidation proposals list \
  --project-id PROJECT --app-id APP --run-id RUN

mem0-sidecar-admin consolidation proposals approve \
  --project-id PROJECT --app-id APP --proposal-id PROPOSAL \
  --expected-status PENDING \
  --expected-source-hashes '{"MEMORY_ID":"SHA256"}'

mem0-sidecar-admin consolidation rollback \
  --project-id PROJECT --app-id APP --proposal-id PROPOSAL

mem0-sidecar-admin consolidation finalize \
  --project-id PROJECT --app-id APP --proposal-id PROPOSAL \
  --confirm-hard-delete
```

Approval enqueues shadowing but never finalizes in the same command. For a
manual `NEAR_DUPLICATE` or `CONTRADICTION`, pass exactly one of
`--canonical-id` or `--replacement-text`.

`scope-backfill` is the only consolidation workflow that writes markers onto
legacy upstream records. It goes through the normal durable `memory.update`
intent/event path, preserves existing metadata, refuses conflicting markers,
and is deliberately operator-triggered rather than part of `OBSERVE` scans.
It refuses to run without a current full-routing bridge heartbeat and an
explicit confirmation that application writes are paused for the maintenance
window. `CONFLICT` and `MISSING` outcomes are persisted per memory and cooled
down before retry, so unresolved records cannot monopolize the front of a
bounded batch. Resume normal writes only after the command finishes.

## Rollback

1. Set `MEM0_SIDECAR_CONSOLIDATION_ENABLED=false` and restart the sidecar.
2. Drain or inspect running consolidation jobs.
3. Run `consolidation rollback` for every `SHADOWED` proposal.
4. Verify search/list counts and pinned memories per app.
5. Restore the sidecar database only if control-plane state is damaged.
6. Keep Mem0 OSS untouched unless restoring from a specific successful export
   checkpoint.

Do not broadly delete or restore the upstream memory store during rollback.
