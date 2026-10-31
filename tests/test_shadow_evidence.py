import hashlib
import json
from pathlib import Path

import pytest

from scripts.verify_shadow_evidence import GATES, REPORTS, SCREENSHOTS, verify

SOURCES = dict(core="a" * 40, sidecar="b" * 40, mcp="c" * 40)


def evidence(
    tmp_path: Path,
    xml: bytes = b'<testsuite><testcase name="live"/></testsuite>',
) -> dict:
    artifacts = {}
    for name in REPORTS | SCREENSHOTS:
        content = xml if name in REPORTS else b"\x89PNG\r\n\x1a\nfixture"
        (tmp_path / name).write_bytes(content)
        artifacts[name] = {
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    manifest = {
        "schema_version": 1,
        "completed": True,
        "sources": SOURCES,
        "completed_gates": sorted(GATES),
        "artifacts": artifacts,
    }
    (tmp_path / "e2e-manifest.json").write_text(json.dumps(manifest))
    return manifest


def test_local_evidence_never_grants_mutation_authority(tmp_path):
    evidence(tmp_path)
    result = verify(tmp_path, SOURCES)
    assert result["local_evidence_valid"]
    assert not result["merge_allowed"]
    assert not result["publish_allowed"]


@pytest.mark.parametrize(
    "xml",
    [
        b"<testsuite/>",
        b"<testsuite><testcase><skipped/></testcase></testsuite>",
        b"<testsuite><testcase><failure/></testcase></testsuite>",
        b'<testsuite errors="1"><testcase/></testsuite>',
    ],
)
def test_empty_skipped_or_failed_suites_are_rejected(tmp_path, xml):
    evidence(tmp_path, xml)
    with pytest.raises(ValueError):
        verify(tmp_path, SOURCES)


def test_stale_missing_and_corrupt_evidence_is_rejected(tmp_path):
    evidence(tmp_path)
    with pytest.raises(ValueError, match="sources"):
        verify(tmp_path, {**SOURCES, "core": "d" * 40})
    (tmp_path / "live-tests.xml").write_text("corrupt")
    with pytest.raises(ValueError, match="Corrupt"):
        verify(tmp_path, SOURCES)
    manifest = evidence(tmp_path)
    manifest["completed_gates"].pop()
    (tmp_path / "e2e-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="gates"):
        verify(tmp_path, SOURCES)


def test_shadow_workflow_has_no_publish_or_merge_credentials():
    workflow_path = Path(__file__).parents[1] / ".github/workflows/upstream-shadow.yml"
    workflow = workflow_path.read_text()
    assert "contents: read" in workflow
    assert ": write" not in workflow
    assert "secrets: inherit" not in workflow
    assert "persist-credentials: false" in workflow
    assert "old-core-new-sidecar" in workflow
    assert "new-core-old-sidecar" in workflow
    assert "new-core-new-sidecar" in workflow
