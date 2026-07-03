import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from mem0_sidecar.store.models import Base


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
