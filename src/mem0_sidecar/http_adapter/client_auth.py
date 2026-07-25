import secrets
from dataclasses import dataclass
from typing import Literal

import httpx


@dataclass(frozen=True)
class ClientPrincipal:
    subject_id: str | None
    role: str
    credential_kind: Literal["bearer", "api_key", "disabled"]


class ClientAuthenticationRejected(RuntimeError):
    def __init__(self, *, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class ClientAuthenticationUnavailable(ClientAuthenticationRejected):
    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            detail="Authentication service is unavailable.",
        )


def _credential_kind(
    authorization: str | None,
    x_api_key: str | None,
) -> Literal["bearer", "api_key", "disabled"]:
    if authorization is not None:
        return "bearer"
    if x_api_key is not None:
        return "api_key"
    return "disabled"


def _redacted_detail(
    value: object,
    *,
    authorization: str | None,
    x_api_key: str | None,
) -> str:
    detail = value if isinstance(value, str) and value else "Authentication failed."
    credential_values = [authorization, x_api_key]
    if authorization is not None:
        _scheme, _separator, token = authorization.partition(" ")
        credential_values.append(token or None)
    for credential in sorted(
        {item for item in credential_values if item},
        key=len,
        reverse=True,
    ):
        detail = detail.replace(credential, "[redacted]")
    return detail


class ClientAuthVerifier:
    def __init__(
        self,
        *,
        enabled: bool,
        base_url: str,
        admin_api_key: str | None,
        auth_path: str = "/auth/me",
        timeout_seconds: float = 5.0,
        allow_bootstrap_admin: bool = True,
        verify_tls: bool = True,
        ca_bundle: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.enabled = enabled
        self.base_url = base_url.rstrip("/")
        self.admin_api_key = admin_api_key
        self.auth_path = f"/{auth_path.lstrip('/')}"
        self.timeout_seconds = timeout_seconds
        self.allow_bootstrap_admin = allow_bootstrap_admin
        self.verify_tls = verify_tls
        self.ca_bundle = ca_bundle
        self.transport = transport

    async def verify(
        self,
        *,
        authorization: str | None,
        x_api_key: str | None,
    ) -> ClientPrincipal:
        if not self.enabled:
            return ClientPrincipal(
                subject_id=None,
                role="system",
                credential_kind="disabled",
            )

        if (
            authorization is None
            and x_api_key is not None
            and self.allow_bootstrap_admin
            and self.admin_api_key is not None
            and secrets.compare_digest(x_api_key, self.admin_api_key)
        ):
            return ClientPrincipal(
                subject_id=None,
                role="admin",
                credential_kind="api_key",
            )

        headers: dict[str, str] = {}
        if authorization is not None:
            headers["Authorization"] = authorization
        if x_api_key is not None:
            headers["X-API-Key"] = x_api_key

        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=httpx.Timeout(self.timeout_seconds),
                verify=self.ca_bundle or self.verify_tls,
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    f"{self.base_url}{self.auth_path}",
                    headers=headers,
                )
        except httpx.RequestError as exc:
            raise ClientAuthenticationUnavailable() from exc

        if response.status_code in {401, 403}:
            try:
                response_body = response.json()
            except ValueError:
                response_body = {}
            detail = (
                response_body.get("detail")
                if isinstance(response_body, dict)
                else None
            )
            raise ClientAuthenticationRejected(
                status_code=response.status_code,
                detail=_redacted_detail(
                    detail,
                    authorization=authorization,
                    x_api_key=x_api_key,
                ),
            )
        if response.status_code != 200:
            raise ClientAuthenticationUnavailable()

        try:
            response_body = response.json()
        except ValueError as exc:
            raise ClientAuthenticationUnavailable() from exc
        if not isinstance(response_body, dict):
            raise ClientAuthenticationUnavailable()
        subject_id = response_body.get("id")
        role = response_body.get("role")
        if not isinstance(subject_id, str) or not subject_id:
            raise ClientAuthenticationUnavailable()
        if not isinstance(role, str) or not role:
            raise ClientAuthenticationUnavailable()

        return ClientPrincipal(
            subject_id=subject_id,
            role=role,
            credential_kind=_credential_kind(authorization, x_api_key),
        )
