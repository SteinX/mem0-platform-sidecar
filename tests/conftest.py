from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from mem0_sidecar.config import load_settings
from mem0_sidecar.store.models import Base


@pytest.fixture(autouse=True)
def disable_client_auth_by_default(monkeypatch) -> Iterator[None]:
    monkeypatch.setenv("MEM0_SIDECAR_CLIENT_AUTH_ENABLED", "false")
    load_settings.cache_clear()
    yield
    load_settings.cache_clear()


@pytest.fixture()
def db_session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
