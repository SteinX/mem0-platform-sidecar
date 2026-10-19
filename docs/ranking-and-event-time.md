# Ranking and event dates

Operator GET/PUT `/v1/projects/{project_id}/ranking-policy` manages a versioned
project policy. PUT takes `expected_version` and `settings`; stale versions
return 409. Default settings disable ranking and access tracking. Example:

```json
{"expected_version":0,"settings":{"enabled":true,"recency_weight":0.15,"access_weight":0.1,"half_life_days":90,"candidate_limit":100}}
```

Weights independently accept 0 through 0.25. Set one to zero to measure the
other. For each scoped candidate, the effective similarity (rerank_score when
present, otherwise score) receives an age penalty approaching recency_weight
and an access bonus of `access_weight * count / (count + 10)`, both relative to
the absolute similarity. Age uses the upstream record's creation time, never
the local import/observation timestamp. Missing creation time gets no age
penalty. Future creation times have age zero. Pinned memories, metadata
`evergreen: true`, decisions, user preferences and project profiles have no age
penalty. No record is deleted or excluded by age/access ranking.

Results retain original score fields and add rank_score/ranking_details with
the separate creation, observation, event and prior access signals. Only memories
actually returned by successful enabled-policy searches increment access_count
and last_accessed_at, atomically and once per distinct ID. Listing, get, failed
searches, candidates trimmed from results and other scopes do not count. Access
tracking leaves projection updated_at and last_observed_at unchanged. Disable
the policy to restore original candidate ordering and stop access tracking;
previous counts are retained. The additive migration initializes counts to zero
without pretending historical observations were accesses.

Add/update metadata `event_date: "YYYY-MM-DD"` records an explicit event date.
Search accepts inclusive `event_from` and `event_to` in that format, independently
of ranking being enabled. Missing or invalid legacy event dates do not match an
event window. Dates are not inferred from prose or import time. New supplied
event dates are validated before writing; no historical backfill occurs.

Ranking/event search operates on a bounded upstream candidate pool (default100,
configurable1..200), expands top_k to that pool and returns the caller's top_k
(0..200; omitted100). Filtering happens within this pool: an empty event result
does not prove the entire collection contains no matching event. Increase the
pool within its bound or use a narrower semantic query. Scope/current-state
filters apply before ranking. Default disabled searches keep their existing
upstream controls and wire shape. This policy is separate from expiration and
consolidation and does not enable either.
