# Scoped extraction policies

Extraction is disabled by default. Upgrade the companion Core SDK/REST server
with `extraction_policy` support before enabling it. Existing category definitions
provide the names and descriptions actually sent to the extraction model. Empty
descriptions fall back to the category name. No extra model call is introduced.

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
    "user_instructions": "Keep verified preferences and explicit project decisions.",
    "agent_instructions": "Keep demonstrated procedures and verified outcomes.",
    "temporary_types": {"temporary": 7}
  }
}
```

Temporary types must reference enabled project categories; TTL is 1-3650 days.
Stale writes return 409. Revert behavior with a new version setting `enabled` to
false. Other project settings remain intact. This changes future adds only.

The existing OSS and platform add paths inject the effective policy. Agent-only
scopes use agent-experience rules; scopes with a user use user-fact rules. Explicit
caller prompt, categories and expiration are preserved. `infer=false` remains a
direct-write bypass. Native Core expiration hides expired facts without deletion;
exact ID reads and clearing the expiration remain available.

Idempotency fingerprints describe the caller request, while mutation intents
retain the actual versioned upstream payload. Retrying an existing key after a
policy update reuses its original result instead of creating a second add.

Classification depends on model quality. Validate positive, irrelevant and
assistant-attribution transcripts using the actual configured model before use.
No production policy is enabled, historic fact rewritten or consolidation mode
changed by installing this code.
