from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-ghcr-images.yml"


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

    assert "repository: mem0ai/mem0" in workflow
    assert "ref: ${{ inputs.mem0_ref || 'main' }}" in workflow
    assert f"{overlay_scripts}/apply-dashboard-overlay" in workflow
    assert f"{overlay_scripts}/verify-dashboard-overlay" in workflow
    assert "mem0-upstream/server/dashboard" in workflow


def test_publish_workflow_tags_release_manual_latest_and_sha():
    workflow = WORKFLOW.read_text()

    assert "type=raw,value=${{ github.event.release.tag_name }}" in workflow
    assert "type=raw,value=${{ inputs.image_tag }}" in workflow
    assert "type=raw,value=latest" in workflow
    assert "type=sha,format=short" in workflow
