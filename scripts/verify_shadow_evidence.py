"""Validate local E2E evidence shape; never authorize a merge or publication."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

GATES = {
    "postgres_migration_smoke",
    "live_service_tests",
    "adoption_test",
    "destructive_browser",
    "mocked_browser",
}
REPORTS = {"live-tests.xml", "adoption-test.xml"}
SCREENSHOTS = {
    f"client-keys-{name}.png"
    for name in (
        "unauthenticated-desktop",
        "create-dialog-desktop",
        "created-copied-desktop",
        "list-desktop",
        "list-compact",
        "revoke-pending-desktop",
    )
}


def verify(directory: Path, sources: dict[str, str]) -> dict[str, object]:
    if set(sources) != {"core", "sidecar", "mcp"} or not all(
        re.fullmatch(r"[0-9a-f]{40}", sha) for sha in sources.values()
    ):
        raise ValueError("Three full source SHAs are required")
    manifest = json.loads((directory / "e2e-manifest.json").read_text())
    if manifest.get("schema_version") != 1 or manifest.get("completed") is not True:
        raise ValueError("Incomplete E2E manifest")
    if manifest.get("sources") != sources:
        raise ValueError("Stale or mismatched sources")
    completed = manifest.get("completed_gates", [])
    if len(completed) != len(GATES) or set(completed) != GATES:
        raise ValueError("Missing or duplicate E2E gates")
    artifacts = manifest.get("artifacts", {})
    if set(artifacts) != REPORTS | SCREENSHOTS:
        raise ValueError("Unexpected or missing evidence artifacts")
    count = 0
    for name, metadata in artifacts.items():
        artifact = directory / name
        if artifact.is_symlink():
            raise ValueError("Evidence symlinks are not accepted")
        content = artifact.read_bytes()
        if (
            len(content) != metadata.get("bytes")
            or hashlib.sha256(content).hexdigest() != metadata.get("sha256")
        ):
            raise ValueError(f"Corrupt evidence: {name}")
        if name in REPORTS:
            root = ET.fromstring(content)
            cases = list(root.iter("testcase"))
            if not cases:
                raise ValueError(f"No test cases: {name}")
            if any(list(root.iter(tag)) for tag in ("skipped", "failure", "error")):
                raise ValueError(f"Non-passing test case: {name}")
            for suite in root.iter("testsuite"):
                if any(
                    int(suite.get(key, "0"))
                    for key in ("skipped", "failures", "errors")
                ):
                    raise ValueError(f"Non-passing suite: {name}")
            count += len(cases)
        elif not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError(f"Invalid screenshot: {name}")
    return {
        "mode": "shadow",
        "sources": sources,
        "tests": count,
        "skipped": 0,
        "local_evidence_valid": True,
        "merge_allowed": False,
        "publish_allowed": False,
        "limitations": [
            "Candidate-generated artifacts are not authenticated attestations",
            "Expected test inventory and trusted check issuer still require "
            "verification",
            "Dev-stack E2E does not qualify release images",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--core", required=True)
    parser.add_argument("--sidecar", required=True)
    parser.add_argument("--mcp", required=True)
    args = parser.parse_args()
    result = verify(
        args.directory,
        {name: getattr(args, name) for name in ("core", "sidecar", "mcp")},
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
