from fastapi.testclient import TestClient

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app


def test_policy_routes_validate_and_reject_stale_versions(tmp_path):
    app = create_app(settings=SidecarSettings(
        database_url=f"sqlite:///{tmp_path / 'sidecar.db'}",
        default_project_id="project-a", mem0_base_url="http://core.invalid",
    ))
    with TestClient(app) as client:
        path = "/v1/projects/project-a/extraction-policy"
        initial = client.get(path).json()
        changed = client.put(path, params={"app_id": "app-a"}, json={
            "expected_version": initial["version"], "settings": {"enabled": True},
        })
        stale = client.put(path, params={"app_id": "app-a"}, json={
            "expected_version": 0, "settings": {"enabled": False},
        })
        unknown_category = client.put(path, json={
            "expected_version": 0, "settings": {"temporary_types": {"foreign": 2}},
        })
        sibling = client.get(path, params={"app_id": "app-b"})
        invalid_app = client.get(path, params={"app_id": " app-a "})
    assert changed.status_code == 200 and changed.json()["version"] == 1
    assert stale.status_code == 409
    assert unknown_category.status_code == 422
    assert sibling.json()["settings"]["enabled"] is False
    assert invalid_app.status_code == 422
