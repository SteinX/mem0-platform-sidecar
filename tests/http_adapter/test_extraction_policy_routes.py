from fastapi.testclient import TestClient

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.core.extraction_policy import (
    ExtractionSettings,
    set_extraction_policy,
)
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.http_adapter.client_auth import (
    ClientAuthenticationRejected,
    ClientPrincipal,
)
from mem0_sidecar.request_attribution import RequestAttribution


class _MemberVerifier:
    async def verify(
        self,
        *,
        authorization: str | None,
        x_api_key: str | None,
        caller_context: str | None = None,
    ) -> ClientPrincipal:
        del x_api_key, caller_context
        if authorization != "Bearer member-token":
            raise ClientAuthenticationRejected(
                status_code=401,
                detail="Authentication required.",
            )
        return ClientPrincipal(
            subject_id="member-1",
            role="member",
            attribution=RequestAttribution(
                transport="rest",
                credential_kind="core_api_key",
                credential_id="e0544e3c-d217-40d9-bc9a-c1f64077542a",
                credential_label="member-test",
                credential_prefix="m0sk_member",
            ),
        )


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


def test_capabilities_are_member_authenticated_scoped_and_redacted(tmp_path):
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.db'}",
            default_project_id="project-a",
            mem0_base_url="http://core.invalid",
            client_auth_enabled=True,
        ),
        client_auth_verifier=_MemberVerifier(),
    )
    with app.state.session_factory() as session:
        set_extraction_policy(
            session,
            project_id="project-a",
            app_id=None,
            expected_version=0,
            settings=ExtractionSettings(
                enabled=True,
                allow_request_profiles=True,
                dual_extraction_enabled=True,
                user_instructions="operator secret instruction",
                agent_instructions="another operator secret",
            ),
        )
        session.commit()

    headers = {"Authorization": "Bearer member-token"}
    with TestClient(app) as client:
        capabilities = client.get("/v1/extraction-capabilities", headers=headers)
        foreign_project = client.get(
            "/v1/extraction-capabilities",
            params={"project_id": "foreign-project"},
            headers=headers,
        )
        foreign_app = client.get(
            "/v1/extraction-capabilities",
            params={"app_id": "foreign-app"},
            headers=headers,
        )
        unauthenticated = client.get("/v1/extraction-capabilities")

    assert capabilities.status_code == 200
    assert capabilities.json() == {
        "protocol": "request-profiles-v1",
        "dual_extraction_enabled": True,
        "request_profiles_enabled": True,
    }
    assert foreign_project.status_code == 403
    assert foreign_app.status_code == 403
    assert unauthenticated.status_code == 401
    public_fields = set(capabilities.json())
    assert not {
        "user_instructions",
        "agent_instructions",
        "categories",
        "secret",
    } & public_fields
