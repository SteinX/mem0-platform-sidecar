from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from mem0_sidecar.http_adapter.client_auth import (
    ClientAuthenticationRejected,
    ClientAuthVerifier,
    ClientPrincipal,
)
from mem0_sidecar.mem0_client.client import Mem0RestClient


def get_session(request: Request) -> Iterator[Session]:
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        yield session


def get_mem0_client(request: Request) -> Mem0RestClient:
    return request.app.state.mem0_client


def get_client_auth_verifier(request: Request) -> ClientAuthVerifier:
    return request.app.state.client_auth_verifier


ClientAuthDependency = Annotated[Any, Depends(get_client_auth_verifier)]


async def require_client_principal(
    request: Request,
    verifier: ClientAuthDependency,
) -> ClientPrincipal:
    try:
        principal = await verifier.verify(
            authorization=request.headers.get("Authorization"),
            x_api_key=request.headers.get("X-API-Key"),
        )
    except ClientAuthenticationRejected as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc
    request.state.client_principal = principal
    return principal
