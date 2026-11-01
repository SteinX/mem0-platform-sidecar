# Scoped extraction policies

Extraction is disabled by default. `enabled`, `allow_request_profiles`, and
`dual_extraction_enabled` are all `false`; instructions default to empty strings.
Upgrade the companion Core SDK/REST server with `extraction_policy` support before
enabling it. No installation changes an existing project policy.

Operator-only endpoints:

- `GET /v1/projects/{project_id}/extraction-policy?app_id=APP`
- `PUT /v1/projects/{project_id}/extraction-policy?app_id=APP`

Omitting `app_id` selects the project default. An app inherits that default until
an override is written. PUT requires the version returned by GET:

```json
{
  "expected_version": 0,
  "settings": {
    "enabled": true,
    "allow_request_profiles": true,
    "dual_extraction_enabled": true,
    "user_instructions": "Keep verified preferences and explicit project decisions.",
    "agent_instructions": "Keep demonstrated procedures and verified outcomes.",
    "temporary_types": {"temporary": 7}
  }
}
```

Temporary types must reference enabled project categories; their TTL is
operator-controlled (1-3650 days). Callers cannot supply category TTLs.

## Request-local profiles

When `allow_request_profiles` is enabled, an add may provide bounded
`custom_instructions` and `agent_custom_instructions`, plus
`custom_categories` as category names or one-name/description mappings:

```json
{
  "custom_categories": ["decision", {"procedure": "A verified procedure"}],
  "custom_instructions": "Keep explicit personal preferences.",
  "agent_custom_instructions": "Keep demonstrated project procedures."
}
```

Non-empty administrator instructions take precedence over caller instructions.
Configured project categories take precedence over request categories, even when
every configured category is disabled; in that case request categories are
suppressed. Request categories are never a project-wide configuration mutation.
The `GET /v1/extraction-capabilities` member route exposes only protocol and
boolean capability flags, never instructions, category definitions, or secrets.
Old or disabled Sidecars that do not advertise `request-profiles-v1` must be
rejected by the bridge before it writes.

Without the new profile fields, legacy single-track prompt precedence is
unchanged. `infer=false` remains a direct-write bypass and does not invoke
extraction policy.

## Optional dual extraction

The bridge's optional dual mode performs two independent durable adds: a
personal child retains `user_id`, while a project child retains `agent_id`.
Both retain the same app, project, and run attribution. It requires
`infer=true`, explicit portable user and agent IDs, a stable root idempotency
key, and both the Sidecar capability and operator dual flags. The first child
uses the root key; the project child uses a derived key. Operators must apply
compatible Sidecar and Core updates before enabling the flag.

The operation is intentionally non-atomic. A partial response is a 409, not a
success acknowledgement, and retrying the identical request reuses durable
intents. The returned composite ID is `dual:<personal UUID>:<project UUID>` and
remains stable across bridge restarts. `list_events` exposes the child rows;
there is no atomic rollback.

Classification depends on model quality. Validate positive, irrelevant, and
assistant-attribution transcripts using the configured model before use. No
production policy is enabled, historic fact rewritten, or consolidation mode
changed by installing this code.
