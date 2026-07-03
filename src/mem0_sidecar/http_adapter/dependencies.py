from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from mem0_sidecar.mem0_client.client import Mem0RestClient


def get_session(request: Request) -> Iterator[Session]:
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        yield session


def get_mem0_client(request: Request) -> Mem0RestClient:
    return request.app.state.mem0_client
