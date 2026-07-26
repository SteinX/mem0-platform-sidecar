import base64
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.http_adapter.client_auth import (
    ClientAuthenticationRejected,
    ClientAuthenticationUnavailable,
    ClientAuthVerifier,
    ClientPrincipal,
)
from mem0_sidecar.request_attribution import RequestAttribution
from mem0_sidecar.store.models import Event


def _response_transport(
    status_code: int,
    payload: dict[str, Any],
    observed: list[httpx.Request] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if observed is not None:
            observed.append(request)
        return httpx.Response(status_code, json=payload, headers=headers)

    return httpx.MockTransport(handler)


def _caller_header(
    *,
    kind: str = "core_api_key",
    label: str = "codex-devbox",
) -> str:
    payload = {
        "v": 1,
        "transport": "mcp",
        "credential_kind": kind,
        "credential_id": (
            "e0544e3c-d217-40d9-bc9a-c1f64077542a"
            if kind == "core_api_key"
            else None
        ),
        "label": label,
        "key_prefix": "m0sk_client_" if kind == "core_api_key" else None,
    }
    serialized = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(serialized).rstrip(b"=").decode("ascii")


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
        attribution=RequestAttribution(
            transport="rest",
            credential_kind="disabled",
        ),
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
                "credential": {
                    "kind": "session",
                    "id": None,
                    "label": None,
                    "key_prefix": None,
                },
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
        attribution=RequestAttribution(
            transport="rest",
            credential_kind="session",
        ),
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
                "credential": {
                    "kind": "core_api_key",
                    "id": "e0544e3c-d217-40d9-bc9a-c1f64077542a",
                    "label": "opencode-devbox",
                    "key_prefix": "m0sk_client_",
                },
            },
            observed,
        ),
    )

    principal = await verifier.verify(
        authorization=None,
        x_api_key="client-api-key",
    )

    assert principal.attribution == RequestAttribution(
        transport="rest",
        credential_kind="core_api_key",
        credential_id="e0544e3c-d217-40d9-bc9a-c1f64077542a",
        credential_label="opencode-devbox",
        credential_prefix="m0sk_client_",
    )
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
        attribution=RequestAttribution(
            transport="rest",
            credential_kind="operator_static",
            credential_label="Legacy admin API key",
        ),
    )


@pytest.mark.asyncio
async def test_trusted_admin_request_accepts_bounded_mcp_caller_context() -> None:
    verifier = ClientAuthVerifier(
        enabled=True,
        base_url="http://mem0.local",
        admin_api_key="internal-admin",
    )

    principal = await verifier.verify(
        authorization=None,
        x_api_key="internal-admin",
        caller_context=_caller_header(),
    )

    assert principal.attribution == RequestAttribution(
        transport="mcp",
        credential_kind="core_api_key",
        credential_id="e0544e3c-d217-40d9-bc9a-c1f64077542a",
        credential_label="codex-devbox",
        credential_prefix="m0sk_client_",
    )


@pytest.mark.asyncio
async def test_malformed_trusted_caller_context_returns_sanitized_400() -> None:
    verifier = ClientAuthVerifier(
        enabled=True,
        base_url="http://mem0.local",
        admin_api_key="internal-admin",
    )

    with pytest.raises(ClientAuthenticationRejected) as captured:
        await verifier.verify(
            authorization=None,
            x_api_key="internal-admin",
            caller_context="m0sk_secret" * 300,
        )

    assert captured.value.status_code == 400
    assert captured.value.detail == "Invalid caller attribution."
    assert "m0sk_secret" not in str(captured.value)


@pytest.mark.asyncio
async def test_untrusted_caller_context_is_ignored_for_normal_core_key() -> None:
    verifier = ClientAuthVerifier(
        enabled=True,
        base_url="http://mem0.local",
        admin_api_key="internal-admin",
        transport=_response_transport(
            200,
            {
                "id": "user-2",
                "role": "member",
                "credential": {
                    "kind": "core_api_key",
                    "id": "12f19718-003a-490d-9fb4-a85e633e1d99",
                    "label": "real-rest-client",
                    "key_prefix": "m0sk_real_cl",
                },
            },
        ),
    )

    principal = await verifier.verify(
        authorization=None,
        x_api_key="normal-client-key",
        caller_context=_caller_header(
            kind="legacy_static",
            label="Legacy shared MCP key",
        ),
    )

    assert principal.attribution.transport == "rest"
    assert principal.attribution.credential_id == (
        "12f19718-003a-490d-9fb4-a85e633e1d99"
    )
    assert principal.attribution.credential_label == "real-rest-client"


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
                "credential": {
                    "kind": "session",
                    "id": None,
                    "label": None,
                    "key_prefix": None,
                },
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
                "credential": {
                    "kind": "disabled",
                    "id": None,
                    "label": None,
                    "key_prefix": None,
                },
            },
        ),
    )

    principal = await verifier.verify(authorization=None, x_api_key=None)

    assert principal.attribution.credential_kind == "disabled"
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
async def test_auth_rejection_preserves_safe_www_authenticate_challenge() -> None:
    verifier = ClientAuthVerifier(
        enabled=True,
        base_url="http://mem0.local",
        admin_api_key=None,
        transport=_response_transport(
            401,
            {"detail": "Authentication required."},
            headers={"WWW-Authenticate": "Bearer"},
        ),
    )

    with pytest.raises(ClientAuthenticationRejected) as captured:
        await verifier.verify(
            authorization=None,
            x_api_key=None,
        )

    assert captured.value.headers == {"WWW-Authenticate": "Bearer"}


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
async def test_oversized_auth_response_fails_closed() -> None:
    def oversized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"x" * (64 * 1024 + 1),
        )

    verifier = ClientAuthVerifier(
        enabled=True,
        base_url="http://mem0.local",
        admin_api_key=None,
        transport=httpx.MockTransport(oversized),
    )

    with pytest.raises(ClientAuthenticationUnavailable):
        await verifier.verify(
            authorization="Bearer client-jwt",
            x_api_key=None,
        )


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
        caller_context: str | None = None,
    ) -> ClientPrincipal:
        del caller_context
        self.calls.append((authorization, x_api_key))
        return ClientPrincipal(
            subject_id="user-4",
            role="member",
            attribution=RequestAttribution(
                transport="rest",
                credential_kind="session",
            ),
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
            "user_id": "u1",
        },
    )

    assert response.status_code == 200
    assert verifier.calls == [("Bearer client-jwt", None)]


def test_trusted_mcp_attribution_is_snapshotted_on_created_event(tmp_path) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            client_auth_enabled=True,
            mem0_api_key="internal-admin",
        ),
        mem0_client=_SearchMem0Client(),
    )

    response = TestClient(app).post(
        "/v3/memories/search/",
        headers={
            "X-API-Key": "internal-admin",
            "X-Mem0-Caller-Context": _caller_header(),
        },
        json={"query": "tea", "user_id": "u1"},
    )

    assert response.status_code == 200
    with app.state.session_factory() as session:
        event = session.scalar(
            select(Event).where(Event.operation == "memory.search")
        )
        assert event is not None
        assert event.request_transport == "mcp"
        assert event.credential_kind == "core_api_key"
        assert event.credential_id == "e0544e3c-d217-40d9-bc9a-c1f64077542a"
        assert event.credential_label == "codex-devbox"
        assert event.credential_prefix == "m0sk_client_"


class _RejectingVerifier:
    async def verify(
        self,
        *,
        authorization: str | None,
        x_api_key: str | None,
        caller_context: str | None = None,
    ) -> ClientPrincipal:
        del caller_context
        raise ClientAuthenticationRejected(
            status_code=401,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
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
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_control_plane_routes_require_client_authentication(tmp_path) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            client_auth_enabled=True,
        ),
        mem0_client=_SearchMem0Client(),
        client_auth_verifier=_RejectingVerifier(),
    )

    response = TestClient(app).get("/v1/projects/default/categories")

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
