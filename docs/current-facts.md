# Current facts and reversible consolidation

Search (`/search`, `/v3/memories/search/`) and compatible listing default to
`current_only: true`: only ACTIVE projections can be returned. JSON search accepts
an explicit boolean `current_only: false` to include retained historical sources
from the same project/app/entity scope in the upstream candidate set. This does
not expand the upstream candidate limit or include expired/deleted records.
Explorer query/scan and exports keep their existing ACTIVE-only behavior.

Newly applied exact/near duplicate proposals mark redundant sources MERGED.
Contradiction proposals mark replaced sources SUPERSEDED. The canonical fact
stays ACTIVE. Other shadow operations and existing historical records keep
SHADOWED. Proposal lifecycle status remains SHADOWED for worker compatibility.
No historical state is rewritten during upgrade.

An ID read of a retained source includes `consolidation` with its state,
proposal, kind, canonical ID and source IDs. Operator proposal listing exposes
the same source/canonical relationship and evidence. Approval, hash/scope/pin
checks, export checkpoints and proposal replay use the existing consolidation
workflow. Rollback restores sources in all three hidden states to ACTIVE.
Neither shadowing nor rollback deletes the upstream records.

Installing this feature does not enable automatic consolidation or hard deletion.
Keep existing policies in OBSERVE until proposal sampling and restore validation
are complete. Use the existing operator CLI for approval/shadow/rollback.
