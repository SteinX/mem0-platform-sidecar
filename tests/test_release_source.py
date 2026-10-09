from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).parents[1] / ".github/workflows/publish-ghcr-images.yml"
TAG = "v0.3.11"


def workflow_script(step_id: str) -> str:
    lines = WORKFLOW.read_text().splitlines()
    start = lines.index(f"        id: {step_id}")
    run = next(i for i in range(start, len(lines)) if lines[i] == "        run: |")
    end = run + 1
    while end < len(lines) and lines[end].startswith("          "):
        end += 1
    return textwrap.dedent("\n".join(lines[run + 1 : end]))


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


@pytest.fixture
def source_repo(tmp_path: Path) -> tuple[Path, str, str]:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Release fixture")
    git(tmp_path, "config", "user.email", "fixture@example.test")
    (tmp_path / "source.txt").write_text("reviewed source\n")
    git(tmp_path, "add", "source.txt")
    git(tmp_path, "commit", "-qm", "reviewed source")
    released = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "tag", TAG)
    (tmp_path / "source.txt").write_text("later default branch source\n")
    git(tmp_path, "commit", "-qam", "later source")
    return tmp_path, released, git(tmp_path, "rev-parse", "HEAD")


def verify_source(path: Path, sha: str, tag: str) -> subprocess.CompletedProcess[str]:
    output = path / "outputs.txt"
    return subprocess.run(
        ["bash", "-c", workflow_script("source")],
        cwd=path,
        env={
            **os.environ,
            "GITHUB_SHA": sha,
            "RELEASE_TAG": tag,
            "GITHUB_OUTPUT": str(output),
            "REQUESTED_CORE_REF": "",
            "DASHBOARD_CORE_REF": "v2.2.1-steinx.1",
        },
        capture_output=True,
        text=True,
    )


def test_release_verifier_accepts_only_the_tagged_source(
    source_repo: tuple[Path, str, str],
) -> None:
    path, released, _ = source_repo
    git(path, "checkout", "-q", TAG)
    assert verify_source(path, released, TAG).returncode == 0
    assert (path / "outputs.txt").read_text().strip() == f"sha={released}"


def test_release_verifier_rejects_default_branch_source(
    source_repo: tuple[Path, str, str],
) -> None:
    path, released, _ = source_repo
    assert verify_source(path, released, TAG).returncode != 0


def test_release_verifier_rejects_mismatched_tag_even_when_event_sha_matches(
    source_repo: tuple[Path, str, str],
) -> None:
    path, _, later = source_repo
    assert verify_source(path, later, TAG).returncode != 0


def test_manual_verifier_binds_to_dispatch_sha(
    source_repo: tuple[Path, str, str],
) -> None:
    path, _, later = source_repo
    assert verify_source(path, later, "").returncode == 0
    assert (path / "outputs.txt").read_text().strip() == f"sha={later}"


def test_workflow_verifies_source_before_publish_credentials() -> None:
    workflow = WORKFLOW.read_text()
    assert "ref: ${{ github.event.release.tag_name || github.sha }}" in workflow
    assert "persist-credentials: false" in workflow
    assert workflow.index("id: source") < workflow.index("- name: Login to GHCR")
    assert workflow.index("id: core-source") < workflow.index("- name: Login to GHCR")
    assert "ref: ${{ env.DASHBOARD_CORE_REF }}" in workflow


def test_manual_publisher_rejects_unreviewed_core_ref(
    source_repo: tuple[Path, str, str],
) -> None:
    path, _, later = source_repo
    result = subprocess.run(
        ["bash", "-c", workflow_script("source")],
        cwd=path,
        env={
            **os.environ,
            "GITHUB_SHA": later,
            "RELEASE_TAG": "",
            "GITHUB_OUTPUT": str(path / "outputs.txt"),
            "REQUESTED_CORE_REF": "main",
            "DASHBOARD_CORE_REF": "v2.2.1-steinx.1",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


@pytest.mark.parametrize("changed", [False, True])
def test_dashboard_source_matches_reviewed_tree(
    source_repo: tuple[Path, str, str], changed: bool
) -> None:
    path, released, _ = source_repo
    reviewed_tree = git(path, "rev-parse", f"{released}^{{tree}}")
    if not changed:
        git(path, "checkout", "-q", TAG)
    parent = path.parent / f"dashboard-{changed}"
    parent.mkdir()
    (parent / "mem0-upstream").symlink_to(path, target_is_directory=True)
    result = subprocess.run(
        ["bash", "-c", workflow_script("core-source")],
        cwd=parent,
        env={
            **os.environ,
            "DASHBOARD_CORE_TREE": reviewed_tree,
            "GITHUB_OUTPUT": str(parent / "outputs.txt"),
        },
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is not changed
