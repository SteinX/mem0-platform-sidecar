from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.http_adapter.client_auth import (
    ClientAuthenticationRejected,
    ClientAuthenticationUnavailable,
    ClientAuthVerifier,
    ClientPrincipal,
)


def _response_transport(
    status_code: int,
    payload: dict[str, Any],
    observed: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if observed is not None:
            observed.append(request)
        return httpx.Response(status_code, json=payload)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_disabled_auth_does_not_contact_core() -> None:
    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("disabled authentication contacted Core")

    verifier = ClientAuthVerifier(
        enabled=False,
        base_url="http://mem0.local",
        admin_api_key=None,
        transport=httpx.MockTransport(unexpected_request),
    )

    principal = await verifier.verify(authorization=None, x_api_key=None)

    assert principal == ClientPrincipal(
        subject_id=None,
        role="system",
        credential_kind="disabled",
    )


@pytest.mark.asyncio
async def test_bearer_auth_delegates_only_the_original_bearer_header() -> None:
    observed: list[httpx.Request] = []
    verifier = ClientAuthVerifier(
        enabled=True,
        base_url="http://mem0.local",
        admin_api_key="internal-admin",
        transport=_response_transport(
            200,
            {
                "id": "user-1",
                "name": "User",
                "email": "user@example.test",
                "role": "member",
                "created_at": "2026-07-25T00:00:00Z",
            },
            observed,
        ),
    )

    principal = await verifier.verify(
        authorization="Bearer client-jwt",
        x_api_key=None,
    )

    assert principal == ClientPrincipal(
        subject_id="user-1",
        role="member",
        credential_kind="bearer",
    )
    assert len(observed) == 1
    assert observed[0].url.path == "/auth/me"
    assert observed[0].headers["Authorization"] == "Bearer client-jwt"
    assert "X-API-Key" not in observed[0].headers


@pytest.mark.asyncio
async def test_user_api_key_auth_delegates_only_the_api_key() -> None:
    observed: list[httpx.Request] = []
    verifier = ClientAuthVerifier(
        enabled=True,
        base_url="http://mem0.local",
        admin_api_key="internal-admin",
        transport=_response_transport(
            200,
            {
                "id": "user-2",
                "name": "User",
                "email": "user@example.test",
                "role": "member",
                "created_at": "2026-07-25T00:00:00Z",
            },
            observed,
        ),
    )

    principal = await verifier.verify(
        authorization=None,
        x_api_key="client-api-key",
    )

    assert principal.credential_kind == "api_key"
    assert observed[0].headers["X-API-Key"] == "client-api-key"
    assert "Authorization" not in observed[0].headers


@pytest.mark.asyncio
async def test_configured_admin_key_supports_empty_user_bootstrap() -> None:
    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("bootstrap admin should not require /auth/me")

    verifier = ClientAuthVerifier(
        enabled=True,
        base_url="http://mem0.local",
        admin_api_key="internal-admin",
        allow_bootstrap_admin=True,
        transport=httpx.MockTransport(unexpected_request),
    )

    principal = await verifier.verify(
        authorization=None,
        x_api_key="internal-admin",
    )

    assert principal == ClientPrincipal(
        subject_id=None,
        role="admin",
        credential_kind="api_key",
    )


@pytest.mark.asyncio
async def test_bearer_takes_precedence_over_configured_admin_key() -> None:
    observed: list[httpx.Request] = []
    verifier = ClientAuthVerifier(
        enabled=True,
        base_url="http://mem0.local",
        admin_api_key="internal-admin",
        transport=_response_transport(
            200,
            {
                "id": "user-3",
                "name": "User",
                "email": "user@example.test",
                "role": "member",
                "created_at": "2026-07-25T00:00:00Z",
            },
            observed,
        ),
    )

    principal = await verifier.verify(
        authorization="Bearer client-jwt",
        x_api_key="internal-admin",
    )

    assert principal.role == "member"
    assert observed[0].headers["Authorization"] == "Bearer client-jwt"
    assert observed[0].headers["X-API-Key"] == "internal-admin"


@pytest.mark.asyncio
async def test_auth_disabled_core_session_is_reported_as_disabled_credential() -> None:
    verifier = ClientAuthVerifier(
        enabled=True,
        base_url="http://mem0.local",
        admin_api_key=None,
        transport=_response_transport(
            200,
            {
                "id": "default-user",
                "name": "Default",
                "email": "default@example.test",
                "role": "admin",
                "created_at": "2026-07-25T00:00:00Z",
            },
        ),
    )

    principal = await verifier.verify(authorization=None, x_api_key=None)

    assert principal.credential_kind == "disabled"
    assert principal.subject_id == "default-user"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_auth_rejection_preserves_status_without_leaking_secrets(
    status_code: int,
) -> None:
    verifier = ClientAuthVerifier(
        enabled=True,
        base_url="http://mem0.local",
        admin_api_key=None,
        transport=_response_transport(
            status_code,
            {"detail": "credential client-jwt is invalid"},
        ),
    )

    with pytest.raises(ClientAuthenticationRejected) as captured:
        await verifier.verify(
            authorization="Bearer client-jwt",
            x_api_key=None,
        )

    assert captured.value.status_code == status_code
    assert "client-jwt" not in captured.value.detail
    assert "client-jwt" not in str(captured.value)


@pytest.mark.asyncio
async def test_auth_transport_failure_becomes_generic_unavailable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("secret-token timed out", request=request)

    verifier = ClientAuthVerifier(
        enabled=True,
        base_url="http://mem0.local",
        admin_api_key=None,
        transport=httpx.MockTransport(timeout),
    )

    with pytest.raises(ClientAuthenticationUnavailable) as captured:
        await verifier.verify(
            authorization="Bearer secret-token",
            x_api_key=None,
        )

    assert captured.value.status_code == 503
    assert "secret-token" not in captured.value.detail
    assert "secret-token" not in str(captured.value)


@pytest.mark.asyncio
async def test_invalid_auth_me_success_payload_fails_closed() -> None:
    verifier = ClientAuthVerifier(
        enabled=True,
        base_url="http://mem0.local",
        admin_api_key=None,
        transport=_response_transport(200, {"role": "member"}),
    )

    with pytest.raises(ClientAuthenticationUnavailable):
        await verifier.verify(
            authorization="Bearer client-jwt",
            x_api_key=None,
        )


class _SearchMem0Client:
    async def search_memories(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"results": [], "payload": payload}


class _RecordingVerifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str | None]] = []

    async def verify(
        self,
        *,
        authorization: str | None,
        x_api_key: str | None,
    ) -> ClientPrincipal:
        self.calls.append((authorization, x_api_key))
        return ClientPrincipal(
            subject_id="user-4",
            role="member",
            credential_kind="bearer",
        )


def test_platform_memory_routes_require_and_attach_client_principal(tmp_path) -> None:
    verifier = _RecordingVerifier()
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            client_auth_enabled=True,
        ),
        mem0_client=_SearchMem0Client(),
        client_auth_verifier=verifier,
    )

    response = TestClient(app).post(
        "/v3/memories/search/",
        headers={"Authorization": "Bearer client-jwt"},
        json={
            "query": "tea",
            "project_id": "default",
            "app_id": "repo",
            "user_id": "u1",
        },
    )

    assert response.status_code == 200
    assert verifier.calls == [("Bearer client-jwt", None)]


class _RejectingVerifier:
    async def verify(
        self,
        *,
        authorization: str | None,
        x_api_key: str | None,
    ) -> ClientPrincipal:
        raise ClientAuthenticationRejected(
            status_code=401,
            detail="Authentication required.",
        )


def test_platform_auth_rejection_uses_fastapi_detail_envelope(tmp_path) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            client_auth_enabled=True,
        ),
        mem0_client=_SearchMem0Client(),
        client_auth_verifier=_RejectingVerifier(),
    )

    response = TestClient(app).post(
        "/v3/memories/search/",
        json={
            "query": "tea",
            "project_id": "default",
            "app_id": "repo",
            "user_id": "u1",
        },
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required."}


def test_health_does_not_invoke_client_auth(tmp_path) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            client_auth_enabled=True,
        ),
        mem0_client=_SearchMem0Client(),
        client_auth_verifier=_RejectingVerifier(),
    )

    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
