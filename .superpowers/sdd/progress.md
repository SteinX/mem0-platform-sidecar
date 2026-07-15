# Dashboard Explorer Phase 2 SDD Progress

Branch start: 324f6956a90387b6a96de4627442d6c1be7f91ea
Plan: /workspace/data/mem0/mem0-platform-sidecar/docs/superpowers/plans/2026-07-13-dashboard-memory-explorer.md

Memory Task 1: complete (commits 324f695..7be7f7e, review clean)
- Minor follow-up: freeze nested metadata filter values or use a frozen value object.
- Minor follow-up: strengthen migration tests to assert ordered columns, uniqueness, and ORM parity.

Memory Task 2: complete (commits 7be7f7e..f554da5, review clean)
- Minor follow-up: prove multiple stale rows share one timestamp and flush exactly once.
- Minor follow-up: add direct coverage for scalar operators, empty filters, multi-metadata paging, and no candidate load above the scan cap.

Memory Task 3: complete (commits f554da5..5e2c851, review clean, no findings)

Memory Task 4: complete (commits 5e2c851..e59825d, review clean after two fix rounds)
- Initial Important findings closed: malformed reconcile safety, category clearing, cutoff CAS, cross-app adoption, bounded hydration, update error classification.
- Follow-up Important findings closed: unconditional projection timestamp touch and atomic unscoped claim.

Memory Task 5: complete (commits e59825d..c2488ad, review clean after two fix rounds)
- Important findings closed: upstream protocol errors remain 5xx, direct JSON decode ValueErrors are wrapped, and update failure audit/stale commits are tested as an intentional transaction exception.

Memory Task 6: complete (commits c2488ad..4adc358, review clean after two fix rounds)
- Important findings closed: strict field/operator/value normalization, strict ISO/range validation, opaque ID stability, and microsecond-precise range ordering aligned with Python semantics.

Memory Task 7: complete (commits 4adc358..b53d7c3, review clean after two fix rounds)
- Important findings closed: deterministic SSR date labels, durable runtime interaction coverage, duplicate filter ID stabilization, and applied-target verifier source integrity.

Memory Task 8: complete (commits b53d7c3..d5fdb34, review clean after two security fix rounds)
- Findings closed: traversal/reserved paths, double-percent FastAPI transport, independent server app scope, zero browser query passthrough, and fail-closed malformed percent/invalid UTF-8 raw paths.

Memory Task 9: complete (commits d5fdb34..dc5ad73, root review clean after one fix round)
- Findings closed: mutation/URL/unmount races, out-of-range pagination, accessible categories, PATCH response convergence, blank IDs, and Next module-ID-0 prerendering via server shell.
- Minor follow-up: add browser-level deferred-promise tests for drawer mutation lifecycle races.

Memory Task 10: complete (commits dc5ad73..1c51e23, root review clean after one fix round)
- Acceptance: focused 132 passed/5 skipped; full 393 passed/5 skipped; live default 11 + adoption 1; zero Docker residue; SQLite migration roundtrip; overlay 19-page build.
- Findings closed: true unscoped counter coverage, tombstone documentation, and primary-error-preserving aggregate cleanup.

Request Task 1: complete (commits 1c51e23..54dd4e7, independent review clean after five hardening rounds)
- Acceptance: focused 72 passed; full 465 passed/5 skipped; Ruff clean; spec and security APPROVED with 0 Critical/Important/Minor.
- Findings closed: compound and normalized credential redaction, zero secret-value reads, strict preview shapes, deterministic bounded mapping traversal, global work budgets, huge integers, typed-key collisions, binary/common Sequence bounds, hostile subclasses, and ambiguous collapsed-key safety.

Request Task 2: complete (commits 54dd4e7..2195ce2, independent review READY after five hardening rounds)
- Acceptance: focused 245 passed; full 541 passed/5 skipped; Ruff/diff clean; spec and security READY with 0 Critical/Important and 2 non-blocking Minor findings.
- Findings closed: reversible 0005 schema and 64-bit counts, independent canonical app/user/agent/run scope, strict result previews, bounded fail-closed legacy parsing, UTC filters, race-safe LIMIT 5001 paging, stable snapshot retry, portable pre-commit scope validation, and bounded credential/URL value scrubbing.
- Minor follow-up: Task 3 defensive serializer should make legacy EventService JSON decoding tolerant; evaluate whether public URL literal `%25` should remain an intentional fail-closed redaction tradeoff.

Request Task 3: complete (commits 2195ce2..4c61d52, independent review READY after two security fix rounds)
- Acceptance: focused 169 passed; full 572 passed/5 skipped; Ruff/diff/worktree clean; spec and security READY with 0 Critical/Important and 1 non-blocking Minor finding.
- Findings closed: tolerant and re-sanitized legacy trace serialization; ADD request correlation; durable SEARCH/GET ALL success and failure traces; filtered result totals; authoritative raw hydration scope validation; 20-item legacy preview cap with correct omitted accounting; and idle-session transaction ownership covering new, dirty, deleted, flushed ORM, and Core DML caller state.
- Minor follow-up: rename the idle-session guard error from “clean session write-set” to “idle session transaction” so the message matches the public contract.

Request Task 4: complete (commits 4c61d52..688c192, independent review READY after one security fix round)
- Acceptance: focused 109 passed before review; final full 625 passed/5 skipped; independent route/repository cap 53 and trace-focused 92 passed; Ruff/diff/worktree clean; spec and security READY with 0 Critical/Important/Minor.
- Findings closed: app-scoped canonical and bounded legacy-null detail lookup with generic 404; exact query/date schemas, duplicate-status rejection, and finite page horizon; app-only resolution within the configured default project; bounded legacy GET with LIMIT 5001 and safe 422; schema-before-project validation; unknown-project non-bootstrap and enumeration resistance; and preserved sanitized legacy envelopes.
- Task 5 integration requirement: every event-detail request must forward the configured app ID.

Request Task 5: complete (commits 688c192..a337d3b, independent review READY after one security fix round)
- Acceptance: overlay scripts 106 passed before review; final Node proxy 35 contracts; full 632 passed/5 skipped; applied dashboard verifier and TypeScript typecheck, Prettier, Ruff, and diff clean; spec and security READY with 0 Critical/Important/Minor.
- Findings closed: exact read-only trace proxy allowlist; portable server-enforced project/app scope on query and detail; allowlist-before-auth-before-config ordering; JSON media-type enforcement and bounded 65,536-byte streaming reads for declared/chunked/invalid UTF-8/cancel-hostile bodies; encoded single-segment ID safety; and runtime-exact closed display operations ADD/SEARCH/GET ALL/UPDATE/DELETE/OTHER with raw operations retained.

Request Task 6: complete (commits a337d3b..11bae03, independent review READY after two UX/accessibility fix rounds)
- Acceptance: final overlay suite 117 passed; full repo before the final focus-only patch 640 passed/5 skipped; applied state harness 5 groups; applied TypeScript typecheck and Next 15 production build 19/19; Ruff, Prettier, diff, and worktree clean; spec and security READY with 0 Critical/Important/Minor.
- Findings closed: Requests timeline/table/drawer overlay; URL/deep-link state, exact AND-only filters, 5,000-row page clamp, server buckets, abort/generation race guards, and responsive states; independent operation and Has Results controls; native keyboard Event actions; shared drawer entity chips; same-ID clipboard generation guard; tamper-resistant verifier wiring; and controlled Sheet focus restoration to connected desktop/mobile openers or the Requests heading for pointer/deep-link fallbacks.

Request Task 7: complete (commits 11bae03..96f6de7, independent review READY after one evidence-strengthening round)
- Acceptance: focused acceptance 502 passed/5 skipped; full 644 passed/5 skipped before witness-only follow-up; final targeted 8 passed/5 skipped; live default 11 and adoption 1; failed and successful Compose paths both zero containers/networks/volumes residue; temporary applied dashboard typecheck/harnesses, Ruff, and diff clean; spec and security READY with 0 Critical/Important and 1 corrected internal-report wording Minor.
- Findings closed: non-tautological negative date witness; correlated 70 KiB ADD preserving a unique prefix and real truncation marker at exactly 4,096 UTF-8 bytes while raw documents remain within 65,536 bytes and secret-free; deterministic live same-project foreign-app GET ALL/detail exclusion with cleanup; nested and normalized credential/internal-URL redaction in public and raw traces; live correlation and scoped ADD/SEARCH/GET ALL proof; and deployment-owned retention/backup sensitivity documentation.

Entity Task 1: complete (commits 96f6de7..7cd2a3a, independent review READY after one concurrency fix round)
- Acceptance: store/migration focused 85 passed; full 657 passed/5 skipped; PostgreSQL lock SQL and SQLite concurrent rebuild probes passed; Ruff/diff/worktree clean; spec and security READY with 0 Critical/Important/Minor.
- Findings closed: reversible 0006 app-scoped entity migration with default/fallback backfill, deterministic legacy dedupe, exact constraint/index/ORM parity, downgrade usability; active-only deterministic projection rebuild and scoped detail/memory ordering; project-row FOR UPDATE before snapshot/delete to serialize same-project rebuilds; missing-project no-scan behavior; and exact ValueError handling for hostile/non-exact entity types.

Entity Task 2: complete (commits 7cd2a3a..d39f064, independent review READY after two security/convergence fix rounds)
- Acceptance: final entities focused 124 and combined re-review focused 246 passed; full before final validation-only patch 792 passed/5 skipped; Ruff/diff/worktree clean; spec and security READY with 0 Critical/Important/Minor.
- Findings closed: strict app/project/type/date/filter entity query and detail; scope-safe per-ID delete with canonical event and projection refresh across memory add/update/delete/reconcile; idempotent upstream 404 convergence after rollback/cancellation/concurrent delete; closed hostile failure classifications without rendering exception names/strings/properties; portable exact project/app/entity/filter ID validation; shared quote-once one-segment Mem0 get/update/delete/history URLs for special IDs; malformed/unhashable/hostile filter fields stable ValueError; caller-owned transaction and Project lock ordering.

Entity Task 3: complete (commits d39f064..c0b9e89, independent review APPROVED after two security/transport fix rounds)
- Acceptance: final route suite 64 passed; direct validation 6 passed; earlier full task baseline 853 passed/5 skipped; Ruff/diff/worktree clean; spec and quality APPROVED with 0 Critical/Important and 3 non-blocking Minor findings.
- Findings closed: validate entity type/ID before project lookup to prevent project-existence disclosure; preserve exact once-decoded literal `%HH` entity IDs across GET/DELETE while retaining malformed-percent, invalid UTF-8, control, and traversal protection; strict query/rebuild schemas, app/project isolation, transaction ownership, and default-project-only administrative bootstrap.
- Minor follow-up: assert full sanitized partial-delete failure entries; protect raw-server startup with cleanup `try/finally`; resolve or narrowly filter the pre-existing Starlette/TestClient deprecation warning.

Entity Task 4: complete (commits c0b9e89..51723a9, independent review APPROVED after one contract fix round)
- Acceptance: proxy harness 40 contracts; overlay script suite 118 passed; exact type focus 1 passed; Prettier/Ruff lint/diff/worktree clean; spec and quality APPROVED with 0 Critical/Important/Minor.
- Findings closed: exact entity query/item proxy allowlist and rebuild exclusion; browser project/app removal and configured scope injection; safe raw segment decode/once-encode for slash and literal-percent IDs; auth/config/scope ordering; exact delete status/result types; nullable `display_name` contract; configured-app GET/DELETE regression coverage.

Entity Task 5: complete (commits 51723a9..0d7759e, independent review APPROVED after three UX/security fix rounds)
- Acceptance: overlay pytest baseline 128 passed plus isolated DNS-interrupted verifier retry; final focused slice 6 passed/123 deselected; fresh apply and full verifier/typecheck; proxy 40, query 11, components 10, memory 11, trace 5 harness contracts; Prettier/Ruff/diff/static checks clean; spec and quality APPROVED with 0 Critical/Important and 2 non-blocking Minor findings.
- Findings closed: full Entities page with shared URL/date/filter state, responsive rows, exact Memory drill-down, detail-before-delete and typed confirmation, terminal outcome UX, no optimistic removal, abort/generation/focus safety; shared single-entity accessible Tooltip without legacy consumer tab-stop regressions; entity-only filter canonicalization; context-transition stale-row delete gating; exact failed IDs; bounded credential sanitizer covering Bearer/Basic/Custom/Digest, comma-separated/quoted parameters, and malformed long input.
- Minor follow-up: strengthen verifier checks for case-insensitive SESSION absence and handler-specific page reset; split the approximately 1,000-line entity page into focused query/delete/presentation units when doing future maintenance.

Entity Task 6: complete (commits 0d7759e..db620a3, independent review APPROVED after two evidence-strengthening rounds)
- Acceptance: focused integration 2 passed; exact acceptance 399 passed/5 expected live skips; full 873 passed/5 skipped; Ruff and applied verifier/typecheck clean; live Compose main 11 + adoption 1; exact project zero container/network/volume/local-image residue; spec, quality, and evidence APPROVED with 0 Critical/Important and 1 non-blocking Minor finding.
- Findings closed: non-tautological counts/all-four-types/max-last-seen/paging/idempotent rebuild/Memory-filter equivalence; successful and partial/retry/final rebuild isolation across same-user foreign app and foreign project using exact Entity signatures; exact sanitized partial response and persisted FAILED entity.delete audit event; real live target-vs-foreign-app upstream survival and finally cleanup; accurate verification-only classification for the tests/docs-only task.
- Minor follow-up: migrate or narrowly suppress the known Starlette/TestClient deprecation warning with an owner/removal condition.

Whole-branch final review: complete (commits 324f695..b0ce6e5, independent review APPROVED after integrated fix wave and browser-detail follow-up)
- Findings closed: one Project->MemoryIndex->Entity mutation lock order; bounded Memory query filters/IN/value/page horizon with true bounded windows; Entity last-seen display; optional Request Trace app fallback; retained PostgreSQL 0004/0005/0006 downgrade/re-upgrade plus real concurrency smoke; retained real Chromium desktop/narrow interaction gate; singular encoded request-detail mock with response-derived sentinels and persistent zero-error diagnostics.
- Final reviewer verdict: 0 Critical/Important/Minor, full spec compliance, security ready, production ready, ready to merge.
- Fresh controller verification: full pytest 888 passed/5 skipped/1 known warning; Ruff clean; disposable applied-overlay typecheck plus proxy 41/category schema 12/category editor 9/query 11/components 11/memory 11/trace 5; live PostgreSQL smoke, main 11, adoption 1, Chromium 40 assertions; exact project zero containers/networks/volumes/images/processes; worktree clean at b0ce6e5.

Continuous review 1: fixes required at b0ce6e5 (0 P1, 4 P2, 2 P3)
- Validated P2s: SQLite mutation lock is ineffective; upstream-success/local-rollback lacks durable recovery; 0005/0006 head-era downgrade round trips lose trace and multi-app entity data; dashboard rejects valid literal-percent memory IDs.
- Controller reproductions: SQLite reconcile/delete bypass; upstream `new` versus projection `old`; head-to-0004-to-head trace reset/entity collapse; `%GG` proxy 403.
- Fix brief: `.superpowers/sdd/continuous-review-1-fix-brief.md`.

Continuous review 1 fix wave: complete at b502a26 (4 P2 fixed; 2 P3 intentionally unchanged)
- P2-1: SQLite no-op project-row UPDATE supplies a cross-process write lock; PostgreSQL retains FOR UPDATE; service-owned projection commits preserve Project -> MemoryIndex -> Entity ordering.
- P2-2: migration 0007 adds bounded sanitized durable mutation intents/targets; cancellation and owning-commit failures recover for add/update/delete/entity delete; add marker replay is deterministic and lossy payloads require exact retry; explicit partial/failed entity outcomes remain terminal.
- P2-3: versioned 0005/0006 compatibility tables preserve exact head-era trace scope/metrics/correlation and per-app Entity rows across head -> 0004 -> head while normally backfilling downgraded-era rows.
- P2-4: browser, Next proxy, FastAPI, and Mem0 transport preserve distinct `a/b`, `a%b`, and literal `a%2Fb` identities for detail/history/update/delete without reopening malformed/traversal/reserved aliases.
- Acceptance: full 906 passed/5 skipped/1 known warning; combined risk gate 617 passed; Ruff/Prettier/diff clean; disposable applied overlay typecheck plus proxy 42/category 12/editor 9/query 11/components 11/memory 11/trace 5; exact live PostgreSQL migration/serialization smoke passed; real Chromium 41 assertions passed; all temporary live resources removed.
- Aggregate Compose note: one cold PostgreSQL readiness race and one Docker BuildKit EOF occurred before application assertions; both exact projects cleaned to zero. The readiness race is retained with a 60-second initialization grace, and PostgreSQL/Chromium affected paths passed independently.
- Report: `.superpowers/sdd/continuous-review-1-fix-report.md`.

Continuous review 1 re-review: fixes required at b502a26 (2 P1, 2 P2, 2 P3)
- P1: content-derived add markers conflate legitimate identical/multi-result adds and recovery deletes valid memories; explicit known failures remain pending and can be silently retried by unrelated mutations with no durable attempt ceiling.
- P2: interrupted SQLite 0005/0006 downgrade can trust an empty compatibility snapshot; 0007 downgrade drops nonterminal recovery intents.
- Closed and preserved: SQLite cross-process mutation serialization and opaque literal-percent ID transport.
- Fix brief: `.superpowers/sdd/continuous-review-1-rereview-fix-brief.md`.

Continuous review 1 re-review fix wave: complete at a58e6aa (2 P1 and 2 P2 fixed; 2 P3 intentionally unchanged)
- P1-1: per-operation random/scoped idempotency markers, optional validated `Idempotency-Key`, unique race handling, persisted safe result reuse, and complete multi-ID adoption replace content identity and destructive dedupe.
- P1-2: explicit ACTIVE/UNKNOWN/COMPLETED/FAILED/PARTIAL/EXHAUSTED states, committed bounded observation claims, expected-effect update proof, exact-target delete observation, and deterministic 409 blocking replace replay recovery.
- P2-1: 0005/0006 CTAS DATA snapshots plus validated READY sentinels rebuild stale artifacts and fail safely before destructive drops or invalid restores.
- P2-2: 0007 refuses downgrade before any drop unless all intents are COMPLETED, FAILED, or PARTIAL; SQLite and PostgreSQL preserve unresolved intent/target rows and revision.
- Preserved: Project-lock preflight waits and rereads live ACTIVE intents before new mutations; SQLite/PostgreSQL serialization and distinct opaque IDs remain green.
- Acceptance: full 943 passed/5 skipped/1 known warning; focused recovery/route/repository/migration/opaque-ID/serialization gates; Ruff/diff/secret scan clean; fresh applied overlay typecheck plus proxy 42/category 12/editor 9/query 11/components 11/memory 11/trace 5; retained PostgreSQL migration/guard/serialization smoke passed.
- Aggregate Compose: one bounded run stopped before service startup on Docker Buildx `panic: send on closed channel`; exact project cleanup independently proved zero containers/networks/volumes/images/processes, so no retry per brief.
- Report: `.superpowers/sdd/continuous-review-1-rereview-fix-report.md`.
