# Upstream compatibility shadow checks

The companion controller lives in `SteinX/mem0` under `.github/UPSTREAM_SHADOW.md`.
This workflow is **manual, read-only and non-publishing**. It does not grant
merge approval, change source pins in the publisher, or update production.

After the infrastructure PR is merged, dispatch `upstream-shadow.yml` from
`main`. Provide complete 40-character SHAs for `core_sha`, `core_base_sha`,
`sidecar_sha`, `sidecar_base_sha`, and `mcp_sha`. The base SHAs should be the
currently released compatible pair. This first version accepts operator-chosen
inputs; it does not authenticate their release provenance automatically.

The serial matrix exercises old-Core/new-sidecar, new-Core/old-sidecar and
new/new. Each job checks out immutable source with credentials disabled, runs
sidecar lint/offline tests, builds native and overlaid dashboards in a temporary
copy, then runs the existing isolated live Compose harness against clean source.
The MCP revision is fixed, not an adaptation target.

The trusted controller checkout validates the existing `e2e-manifest.json`:
exact Core/sidecar/MCP source SHAs, all required gate names, expected artifact
names/checksums and nonempty JUnit reports with no failures, errors or skips.
Artifacts from failed jobs are diagnostic, never evidence of success. No cloud
LLM credentials, production URLs, publish token or merge token are supplied.

The harness's deterministic OpenAI stub does not measure real-model recall or
extraction quality. Its dev Core image does not qualify the published release
image. Checksummed candidate-generated evidence is also not an authenticated
attestation; expected test inventory and check issuer verification remain future
release gates. The verifier explicitly returns `merge_allowed: false` and
`publish_allowed: false` even when local evidence is valid.

The workflow's default-branch restriction prevents pre-merge activation. Existing
CI runs the new validator's negative tests on PRs; the costly compatibility
matrix runs only when explicitly dispatched after merge. This infrastructure PR
does not itself prove a new upstream version is compatible.

Local validator tests:

```sh
python -m pip install '.[dev]'
python -m ruff check scripts/verify_shadow_evidence.py tests/test_shadow_evidence.py
python -m pytest -q tests/test_shadow_evidence.py
```

Before dispatch, review all candidate code: it executes on a disposable GitHub
hosted runner without production or write credentials. Never change this into a
privileged `pull_request_target` job or run these inputs on a production runner.
