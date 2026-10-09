import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-ghcr-images.yml"


def test_publication_workflow_parses_with_top_level_source_pin():
    workflow = yaml.safe_load(WORKFLOW.read_text())

    assert isinstance(workflow, dict)
    environment = workflow["env"]
    assert isinstance(environment, dict)
    assert re.fullmatch(r"[0-9a-f]{40}", environment["DASHBOARD_CORE_TREE"])
    assert isinstance(workflow["jobs"]["publish"]["steps"], list)


def test_publish_workflow_publishes_sidecar_and_dashboard_images():
    workflow = WORKFLOW.read_text()

    assert "release:" in workflow
    assert "types: [published]" in workflow
    assert "workflow_dispatch:" in workflow
    assert "packages: write" in workflow
    assert "contents: read" in workflow

    assert "ghcr.io/steinx/mem0-platform-sidecar" in workflow
    assert "ghcr.io/steinx/mem0-dashboard-sidecar" in workflow
    assert workflow.count("docker/build-push-action@v6") == 2
    assert "file: docker/Dockerfile" in workflow
    assert "file: mem0-upstream/server/dashboard/Dockerfile" in workflow
    assert "context: mem0-upstream/server/dashboard" in workflow


def test_publish_workflow_applies_and_verifies_dashboard_overlay():
    workflow = WORKFLOW.read_text()
    overlay_scripts = "integrations/mem0-dashboard-overlay/scripts"

    assert "repository: SteinX/mem0" in workflow
    assert "ref: ${{ env.DASHBOARD_CORE_REF }}" in workflow
    assert "DASHBOARD_CORE_REF: v2.2.1-steinx.1" in workflow
    assert f"{overlay_scripts}/apply-dashboard-overlay" in workflow
    assert f"{overlay_scripts}/verify-dashboard-overlay" in workflow
    assert "mem0-upstream/server/dashboard" in workflow


def test_publish_workflow_exposes_checked_out_dashboard_to_tests():
    workflow = WORKFLOW.read_text()

    assert (
        "MEM0_UPSTREAM_DASHBOARD: "
        "${{ github.workspace }}/mem0-upstream/server/dashboard"
    ) in workflow


def test_publish_workflow_installs_dashboard_dependencies_before_tests():
    workflow = WORKFLOW.read_text()

    assert workflow.index("- name: Set up Node.js") < workflow.index(
        "- name: Run sidecar tests"
    )
    assert workflow.index("- name: Install dashboard dependencies") < workflow.index(
        "- name: Run sidecar tests"
    )


def test_publish_workflow_tags_release_manual_latest_and_sha():
    workflow = WORKFLOW.read_text()

    assert "type=raw,value=${{ github.event.release.tag_name }}" in workflow
    assert "type=raw,value=${{ inputs.image_tag }}" in workflow
    assert "type=raw,value=latest" in workflow
    assert "type=sha,format=short" in workflow


def test_prereleases_preserve_latest_for_both_images() -> None:
    workflow = WORKFLOW.read_text()
    assert (
        "github.event_name == 'release' && !github.event.release.prerelease" in workflow
    )
    assert "github.event_name == 'workflow_dispatch' && inputs.push_latest" in workflow
    assert workflow.count("type=raw,value=latest,enable=${{ env.PUSH_LATEST }}") == 2
