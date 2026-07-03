# Mem0 Platform Sidecar Phase 0-1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bootstrap the `mem0-platform-sidecar` service repository and implement the first durable control-plane core for projects, API keys, categories, memory index projections, events, entities, and jobs.

**Architecture:** The sidecar is a Python/FastAPI service with a SQLite-first control-plane database. Mem0 OSS remains the data-plane memory engine; the sidecar owns project/category/event/job/entity state and talks to Mem0 OSS only through REST. HTTP Platform compatibility and MCP compatibility receive dedicated follow-up plans after this core is working and tested.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2.x, Alembic, Pydantic Settings, httpx, pytest, SQLite.

## Global Constraints

- Do not patch Mem0 OSS.
- Do not write directly to Mem0 OSS internal database tables.
- Treat Mem0 OSS REST as the only data-plane contract.
- Preserve `user_id`, `agent_id`, `app_id`, and `run_id` as first-class scope fields.
- Keep `app_id` project isolation intact; do not collapse unrelated workspaces into a default namespace.
- Store durable events for mutating sidecar operations even when the underlying operation is synchronous.
- Use SQLite first, with schema and repository boundaries that can support Postgres.
- Keep MCP handlers independent from HTTP route functions.
- Keep HTTP route functions independent from core service implementation details.
- The current planning session must not create a commit. Commit steps below are for future execution after the user chooses implementation.

---

## Scope Check

This plan covers Phase 0 and Phase 1 from the design spec:

- Phase 0: repository bootstrap, configuration, basic app wiring, development tooling.
- Phase 1: control-plane database models, repositories, core services, durable event/job/entity/category/index behavior.

The following design areas are intentionally excluded from this plan and need separate plans:

- Platform-compatible `/v3/memories/*` HTTP endpoints.
- Hosted-MCP-compatible tools.
- Codex/OpenCode/OpenClaw/Hermes plugin smoke tests.
- Export/import job payload formats.
- Webhooks, dashboard, analytics, and operational UI.

## File Structure

Create this repository structure under `/workspace/data/mem0/mem0-platform-sidecar`:

```text
.
  pyproject.toml
  .gitignore
  README.md
  docker/
    Dockerfile
    docker-compose.dev.yml
  migrations/
    env.py
    versions/
      0001_control_plane_core.py
  docs/
    development.md
    superpowers/
      specs/
        2026-07-03-mem0-platform-sidecar-design.md
      plans/
        2026-07-04-mem0-platform-sidecar-phase0-phase1.md
  src/
    mem0_sidecar/
      __init__.py
      config.py
      core/
        __init__.py
        categories.py
        events.py
        projects.py
        scope.py
      http_adapter/
        __init__.py
        app.py
      mem0_client/
        __init__.py
        client.py
      store/
        __init__.py
        database.py
        models.py
        repositories.py
      workers/
        __init__.py
        runner.py
  tests/
    conftest.py
    test_package.py
    test_config.py
    core/
      test_categories.py
      test_events.py
      test_scope.py
    http_adapter/
      test_health.py
    mem0_client/
      test_client.py
    store/
      test_models.py
      test_repositories.py
    workers/
      test_runner.py
    integration/
      test_control_plane_flow.py
```

## Task 1: Repository Bootstrap

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `README.md`
- Create: `src/mem0_sidecar/__init__.py`
- Create: `tests/test_package.py`

**Interfaces:**
- Produces: importable package `mem0_sidecar`
- Produces: `mem0_sidecar.__version__: str`
- Consumes: none

- [ ] **Step 1: Write the failing package import test**

Create `tests/test_package.py`:

```python
import mem0_sidecar


def test_package_has_version() -> None:
    assert mem0_sidecar.__version__ == "0.1.0"
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
cd /workspace/data/mem0/mem0-platform-sidecar
python -m pytest tests/test_package.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'mem0_sidecar'`.

- [ ] **Step 3: Create project metadata and package init**

Create `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=69", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "mem0-platform-sidecar"
version = "0.1.0"
description = "Control-plane sidecar for Mem0 OSS Platform compatibility"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.111,<1.0",
  "uvicorn[standard]>=0.30,<1.0",
  "sqlalchemy>=2.0,<3.0",
  "alembic>=1.13,<2.0",
  "pydantic>=2.7,<3.0",
  "pydantic-settings>=2.3,<3.0",
  "httpx>=0.27,<1.0",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.2,<9.0",
  "pytest-asyncio>=0.23,<1.0",
  "ruff>=0.5,<1.0",
]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
asyncio_mode = "auto"

[tool.ruff]
line-length = 88
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
```

Create `.gitignore`:

```gitignore
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
.mypy_cache/
.coverage
htmlcov/
*.sqlite
*.sqlite3
.env
```

Create `README.md`:

```markdown
# Mem0 Platform Sidecar

Control-plane sidecar for Mem0 OSS Platform compatibility.

This service owns projects, API keys, categories, memory index projections,
events, entities, and jobs. Mem0 OSS remains the memory data plane and is
accessed through REST APIs.
```

Create `src/mem0_sidecar/__init__.py`:

```python
__version__ = "0.1.0"
```

- [ ] **Step 4: Run the test to verify it passes**

Run:

```bash
cd /workspace/data/mem0/mem0-platform-sidecar
python -m pytest tests/test_package.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run only during future implementation:

```bash
git add pyproject.toml .gitignore README.md src/mem0_sidecar/__init__.py tests/test_package.py
git commit -m "chore: bootstrap sidecar package"
```

## Task 2: Settings And Configuration

**Files:**
- Create: `src/mem0_sidecar/config.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Consumes: package from Task 1
- Produces: `SidecarSettings`
- Produces: `load_settings() -> SidecarSettings`

- [ ] **Step 1: Write failing configuration tests**

Create `tests/test_config.py`:

```python
from mem0_sidecar.config import SidecarSettings, load_settings


def test_settings_defaults_use_local_development_values(monkeypatch) -> None:
    monkeypatch.delenv("MEM0_SIDECAR_DATABASE_URL", raising=False)
    monkeypatch.delenv("MEM0_SIDECAR_MEM0_BASE_URL", raising=False)

    settings = load_settings()

    assert settings.database_url == "sqlite:///./mem0_sidecar.sqlite3"
    assert settings.mem0_base_url == "http://127.0.0.1:8000"
    assert settings.default_project_id == "default"


def test_settings_can_be_loaded_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("MEM0_SIDECAR_DATABASE_URL", "sqlite:///tmp/test.sqlite3")
    monkeypatch.setenv("MEM0_SIDECAR_MEM0_BASE_URL", "http://mem0:8000")
    monkeypatch.setenv("MEM0_SIDECAR_DEFAULT_PROJECT_ID", "repo-a")

    settings = SidecarSettings()

    assert settings.database_url == "sqlite:///tmp/test.sqlite3"
    assert settings.mem0_base_url == "http://mem0:8000"
    assert settings.default_project_id == "repo-a"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python -m pytest tests/test_config.py -v
```

Expected: FAIL with `ModuleNotFoundError` or missing `config` module.

- [ ] **Step 3: Implement settings**

Create `src/mem0_sidecar/config.py`:

```python
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SidecarSettings(BaseSettings):
    database_url: str = Field(default="sqlite:///./mem0_sidecar.sqlite3")
    mem0_base_url: str = Field(default="http://127.0.0.1:8000")
    mem0_api_key: str | None = Field(default=None)
    default_project_id: str = Field(default="default")
    worker_poll_interval_seconds: float = Field(default=1.0, ge=0.1)

    model_config = SettingsConfigDict(
        env_prefix="MEM0_SIDECAR_",
        env_file=".env",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def load_settings() -> SidecarSettings:
    return SidecarSettings()
```

- [ ] **Step 4: Run tests to verify pass**

Run:

```bash
python -m pytest tests/test_config.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run only during future implementation:

```bash
git add src/mem0_sidecar/config.py tests/test_config.py
git commit -m "feat: add sidecar settings"
```

## Task 3: Database Session And Control-Plane Models

**Files:**
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/versions/0001_control_plane_core.py`
- Create: `src/mem0_sidecar/store/__init__.py`
- Create: `src/mem0_sidecar/store/database.py`
- Create: `src/mem0_sidecar/store/models.py`
- Create: `tests/conftest.py`
- Create: `tests/store/test_models.py`

**Interfaces:**
- Consumes: `SidecarSettings.database_url`
- Produces: `Base`
- Produces: `create_engine_from_url(database_url: str) -> Engine`
- Produces: SQLAlchemy models `Project`, `ApiKey`, `Category`, `MemoryIndex`, `Event`, `Entity`, `Job`
- Produces: Alembic baseline migration `0001_control_plane_core`

- [ ] **Step 1: Write failing model persistence tests**

Create `tests/conftest.py`:

```python
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
```

Create `tests/store/test_models.py`:

```python
from mem0_sidecar.store.models import (
    Category,
    Event,
    EventStatus,
    Job,
    JobStatus,
    MemoryIndex,
    Project,
)


def test_control_plane_models_persist(db_session) -> None:
    project = Project(
        id="repo-a",
        name="Repo A",
        default_user_id="root",
        default_app_id="repo-a",
        default_agent_id="codex",
        mem0_base_url="http://mem0:8000",
    )
    db_session.add(project)
    db_session.add(Category(project_id="repo-a", name="decision", description="Architecture decisions"))
    db_session.add(
        MemoryIndex(
            project_id="repo-a",
            mem0_memory_id="mem-1",
            user_id="root",
            app_id="repo-a",
            category="decision",
        )
    )
    event = Event(
        project_id="repo-a",
        operation="memory.add",
        status=EventStatus.SUCCEEDED,
        subject_type="memory",
        subject_id="mem-1",
    )
    db_session.add(event)
    db_session.flush()
    db_session.add(Job(project_id="repo-a", event_id=event.id, job_type="entity.rebuild", status=JobStatus.PENDING))
    db_session.commit()

    assert db_session.get(Project, "repo-a").default_app_id == "repo-a"
    assert db_session.query(Category).filter_by(project_id="repo-a").one().name == "decision"
    assert db_session.query(MemoryIndex).filter_by(mem0_memory_id="mem-1").one().category == "decision"
    assert db_session.query(Event).filter_by(subject_id="mem-1").one().status is EventStatus.SUCCEEDED
    assert db_session.query(Job).filter_by(project_id="repo-a").one().job_type == "entity.rebuild"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python -m pytest tests/store/test_models.py -v
```

Expected: FAIL with missing store modules or model classes.

- [ ] **Step 3: Implement database helpers**

Create `src/mem0_sidecar/store/__init__.py`:

```python
"""Persistence layer for the Mem0 Platform sidecar."""
```

Create `src/mem0_sidecar/store/database.py`:

```python
from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def create_engine_from_url(database_url: str) -> Engine:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args, future=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def iter_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session
```

- [ ] **Step 4: Implement models**

Create `src/mem0_sidecar/store/models.py`:

```python
from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class EventStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    default_user_id: Mapped[str | None] = mapped_column(String(256))
    default_app_id: Mapped[str | None] = mapped_column(String(256))
    default_agent_id: Mapped[str | None] = mapped_column(String(256))
    mem0_base_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    mem0_api_key_ref: Mapped[str | None] = mapped_column(String(256))
    settings_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    scopes_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    schema_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    strategy: Mapped[str] = mapped_column(String(64), default="metadata", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class MemoryIndex(Base):
    __tablename__ = "memories_index"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    mem0_memory_id: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(256))
    agent_id: Mapped[str | None] = mapped_column(String(256))
    app_id: Mapped[str | None] = mapped_column(String(256))
    run_id: Mapped[str | None] = mapped_column(String(256))
    category: Mapped[str | None] = mapped_column(String(128))
    entity_refs_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    metadata_projection_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[EventStatus] = mapped_column(SAEnum(EventStatus), default=EventStatus.PENDING, nullable=False)
    subject_type: Mapped[str | None] = mapped_column(String(128))
    subject_id: Mapped[str | None] = mapped_column(String(256))
    request_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    response_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    error_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Entity(Base):
    __tablename__ = "entities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(256))
    metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    memory_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    event_id: Mapped[str | None] = mapped_column(ForeignKey("events.id"))
    job_type: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[JobStatus] = mapped_column(SAEnum(JobStatus), default=JobStatus.PENDING, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    run_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
```

- [ ] **Step 5: Add Alembic baseline migration**

Create `alembic.ini`:

```ini
[alembic]
script_location = migrations
prepend_sys_path = .
sqlalchemy.url = sqlite:///./mem0_sidecar.sqlite3

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

Create `migrations/env.py`:

```python
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from mem0_sidecar.store.models import Base

config = context.config

database_url = os.getenv("MEM0_SIDECAR_DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

Create `migrations/versions/0001_control_plane_core.py`:

```python
from alembic import op
import sqlalchemy as sa

revision = "0001_control_plane_core"
down_revision = None
branch_labels = None
depends_on = None

event_status = sa.Enum("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", name="eventstatus")
job_status = sa.Enum("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", name="jobstatus")


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("default_user_id", sa.String(length=256)),
        sa.Column("default_app_id", sa.String(length=256)),
        sa.Column("default_agent_id", sa.String(length=256)),
        sa.Column("mem0_base_url", sa.String(length=1024), nullable=False),
        sa.Column("mem0_api_key_ref", sa.String(length=256)),
        sa.Column("settings_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("key_hash", sa.String(length=128), nullable=False, unique=True),
        sa.Column("prefix", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=128), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("scopes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "categories",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=128), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("schema_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("strategy", sa.String(length=64), nullable=False, server_default="metadata"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "memories_index",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=128), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("mem0_memory_id", sa.String(length=256), nullable=False, unique=True),
        sa.Column("user_id", sa.String(length=256)),
        sa.Column("agent_id", sa.String(length=256)),
        sa.Column("app_id", sa.String(length=256)),
        sa.Column("run_id", sa.String(length=256)),
        sa.Column("category", sa.String(length=128)),
        sa.Column("entity_refs_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("metadata_projection_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=128), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("operation", sa.String(length=128), nullable=False),
        sa.Column("status", event_status, nullable=False),
        sa.Column("subject_type", sa.String(length=128)),
        sa.Column("subject_id", sa.String(length=256)),
        sa.Column("request_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("response_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("error_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "entities",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=128), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=256), nullable=False),
        sa.Column("display_name", sa.String(length=256)),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("memory_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=128), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("event_id", sa.String(length=36), sa.ForeignKey("events.id")),
        sa.Column("job_type", sa.String(length=128), nullable=False),
        sa.Column("status", job_status, nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("run_after", sa.DateTime(timezone=True)),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("jobs")
    op.drop_table("entities")
    op.drop_table("events")
    op.drop_table("memories_index")
    op.drop_table("categories")
    op.drop_table("api_keys")
    op.drop_table("projects")
```

- [ ] **Step 6: Run Alembic upgrade on a scratch database**

Run:

```bash
MEM0_SIDECAR_DATABASE_URL=sqlite:///./scratch.sqlite3 python -m alembic upgrade head
```

Expected: command exits 0 and creates `scratch.sqlite3`.

- [ ] **Step 7: Run tests to verify pass**

Run:

```bash
python -m pytest tests/store/test_models.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

Run only during future implementation:

```bash
git add alembic.ini migrations src/mem0_sidecar/store tests/conftest.py tests/store/test_models.py
git commit -m "feat: add control plane models"
```

## Task 4: Scope Normalization

**Files:**
- Create: `src/mem0_sidecar/core/__init__.py`
- Create: `src/mem0_sidecar/core/scope.py`
- Create: `tests/core/test_scope.py`

**Interfaces:**
- Consumes: none
- Produces: `Scope`
- Produces: `normalize_scope(*, project_id: str, user_id: str | None, app_id: str | None, agent_id: str | None, run_id: str | None) -> Scope`
- Produces: `Scope.as_filter_dict() -> dict[str, str]`

- [ ] **Step 1: Write failing scope tests**

Create `tests/core/test_scope.py`:

```python
from mem0_sidecar.core.scope import normalize_scope


def test_normalize_scope_preserves_first_class_fields() -> None:
    scope = normalize_scope(
        project_id="repo-a",
        user_id="root",
        app_id=None,
        agent_id="codex",
        run_id="session-1",
    )

    assert scope.project_id == "repo-a"
    assert scope.user_id == "root"
    assert scope.app_id == "repo-a"
    assert scope.agent_id == "codex"
    assert scope.run_id == "session-1"
    assert scope.as_filter_dict() == {
        "user_id": "root",
        "agent_id": "codex",
        "app_id": "repo-a",
        "run_id": "session-1",
    }
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python -m pytest tests/core/test_scope.py -v
```

Expected: FAIL with missing `mem0_sidecar.core.scope`.

- [ ] **Step 3: Implement scope normalization**

Create `src/mem0_sidecar/core/__init__.py`:

```python
"""Core domain services for the Mem0 Platform sidecar."""
```

Create `src/mem0_sidecar/core/scope.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Scope:
    project_id: str
    user_id: str | None
    app_id: str
    agent_id: str | None
    run_id: str | None

    def as_filter_dict(self) -> dict[str, str]:
        values = {
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "app_id": self.app_id,
            "run_id": self.run_id,
        }
        return {key: value for key, value in values.items() if value}


def normalize_scope(
    *,
    project_id: str,
    user_id: str | None,
    app_id: str | None,
    agent_id: str | None,
    run_id: str | None,
) -> Scope:
    normalized_app_id = app_id or project_id
    return Scope(
        project_id=project_id,
        user_id=user_id,
        app_id=normalized_app_id,
        agent_id=agent_id,
        run_id=run_id,
    )
```

- [ ] **Step 4: Run tests to verify pass**

Run:

```bash
python -m pytest tests/core/test_scope.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run only during future implementation:

```bash
git add src/mem0_sidecar/core tests/core/test_scope.py
git commit -m "feat: normalize sidecar scopes"
```

## Task 5: Repository Layer

**Files:**
- Create: `src/mem0_sidecar/store/repositories.py`
- Create: `tests/store/test_repositories.py`

**Interfaces:**
- Consumes: SQLAlchemy models from Task 3
- Produces: `ProjectRepository.upsert_default_project(...) -> Project`
- Produces: `CategoryRepository.replace_project_categories(...) -> list[Category]`
- Produces: `EventRepository.create_event(...) -> Event`
- Produces: `EventRepository.mark_succeeded(...) -> Event`
- Produces: `MemoryIndexRepository.upsert_memory(...) -> MemoryIndex`
- Produces: `EntityRepository.upsert_entity(...) -> Entity`
- Produces: `JobRepository.enqueue(...) -> Job`
- Produces: `JobRepository.claim_next() -> Job | None`

- [ ] **Step 1: Write failing repository tests**

Create `tests/store/test_repositories.py`:

```python
from mem0_sidecar.store.models import EventStatus, JobStatus
from mem0_sidecar.store.repositories import (
    CategoryRepository,
    EntityRepository,
    EventRepository,
    JobRepository,
    MemoryIndexRepository,
    ProjectRepository,
)


def test_repositories_support_control_plane_flow(db_session) -> None:
    project_repo = ProjectRepository(db_session)
    category_repo = CategoryRepository(db_session)
    event_repo = EventRepository(db_session)
    memory_repo = MemoryIndexRepository(db_session)
    entity_repo = EntityRepository(db_session)
    job_repo = JobRepository(db_session)

    project = project_repo.upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
        default_user_id="root",
        default_agent_id="codex",
    )
    categories = category_repo.replace_project_categories(
        project_id=project.id,
        categories=[{"name": "decision", "description": "Architecture decisions"}],
    )
    event = event_repo.create_event(project_id=project.id, operation="memory.add")
    memory = memory_repo.upsert_memory(
        project_id=project.id,
        mem0_memory_id="mem-1",
        user_id="root",
        app_id="repo-a",
        category="decision",
        metadata={"type": "decision"},
    )
    entity = entity_repo.upsert_entity(
        project_id=project.id,
        entity_type="app",
        entity_id="repo-a",
        display_name="Repo A",
    )
    job = job_repo.enqueue(project_id=project.id, event_id=event.id, job_type="entity.rebuild", payload={})
    event_repo.mark_succeeded(event.id, response={"memory_id": memory.mem0_memory_id})
    db_session.commit()

    assert categories[0].name == "decision"
    assert event_repo.get(event.id).status is EventStatus.SUCCEEDED
    assert memory.category == "decision"
    assert entity.memory_count == 0
    assert job.status is JobStatus.PENDING
    assert job_repo.claim_next().id == job.id
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python -m pytest tests/store/test_repositories.py -v
```

Expected: FAIL with missing repository classes.

- [ ] **Step 3: Implement repositories**

Create `src/mem0_sidecar/store/repositories.py`:

```python
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mem0_sidecar.store.models import (
    Category,
    Entity,
    Event,
    EventStatus,
    Job,
    JobStatus,
    MemoryIndex,
    Project,
)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProjectRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_default_project(
        self,
        *,
        project_id: str,
        name: str,
        mem0_base_url: str,
        default_user_id: str | None = None,
        default_agent_id: str | None = None,
    ) -> Project:
        project = self.session.get(Project, project_id)
        if project is None:
            project = Project(
                id=project_id,
                name=name,
                default_user_id=default_user_id,
                default_app_id=project_id,
                default_agent_id=default_agent_id,
                mem0_base_url=mem0_base_url,
            )
            self.session.add(project)
        else:
            project.name = name
            project.mem0_base_url = mem0_base_url
            project.default_user_id = default_user_id
            project.default_agent_id = default_agent_id
            project.default_app_id = project_id
        self.session.flush()
        return project


class CategoryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def replace_project_categories(self, *, project_id: str, categories: list[dict[str, Any]]) -> list[Category]:
        existing = self.session.scalars(select(Category).where(Category.project_id == project_id)).all()
        for category in existing:
            self.session.delete(category)
        created: list[Category] = []
        for item in categories:
            category = Category(
                project_id=project_id,
                name=str(item["name"]),
                description=str(item.get("description", "")),
                schema_json=_json(item.get("schema", {})),
                enabled=1,
                strategy=str(item.get("strategy", "metadata")),
            )
            self.session.add(category)
            created.append(category)
        self.session.flush()
        return created

    def list_project_categories(self, project_id: str) -> list[Category]:
        return list(self.session.scalars(select(Category).where(Category.project_id == project_id)))


class EventRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_event(
        self,
        *,
        project_id: str,
        operation: str,
        request: dict[str, Any] | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
    ) -> Event:
        event = Event(
            project_id=project_id,
            operation=operation,
            status=EventStatus.PENDING,
            subject_type=subject_type,
            subject_id=subject_id,
            request_json=_json(request or {}),
        )
        self.session.add(event)
        self.session.flush()
        return event

    def get(self, event_id: str) -> Event:
        event = self.session.get(Event, event_id)
        if event is None:
            raise KeyError(event_id)
        return event

    def mark_succeeded(self, event_id: str, *, response: dict[str, Any]) -> Event:
        event = self.get(event_id)
        event.status = EventStatus.SUCCEEDED
        event.response_json = _json(response)
        event.completed_at = _utc_now()
        self.session.flush()
        return event

    def mark_failed(self, event_id: str, *, error: dict[str, Any]) -> Event:
        event = self.get(event_id)
        event.status = EventStatus.FAILED
        event.error_json = _json(error)
        event.completed_at = _utc_now()
        self.session.flush()
        return event


class MemoryIndexRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_memory(
        self,
        *,
        project_id: str,
        mem0_memory_id: str,
        user_id: str | None,
        app_id: str | None,
        category: str | None,
        agent_id: str | None = None,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryIndex:
        memory = self.session.scalar(
            select(MemoryIndex).where(MemoryIndex.mem0_memory_id == mem0_memory_id)
        )
        if memory is None:
            memory = MemoryIndex(project_id=project_id, mem0_memory_id=mem0_memory_id)
            self.session.add(memory)
        memory.user_id = user_id
        memory.agent_id = agent_id
        memory.app_id = app_id
        memory.run_id = run_id
        memory.category = category
        memory.metadata_projection_json = _json(metadata or {})
        memory.deleted_at = None
        self.session.flush()
        return memory


class EntityRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_entity(
        self,
        *,
        project_id: str,
        entity_type: str,
        entity_id: str,
        display_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Entity:
        entity = self.session.scalar(
            select(Entity).where(
                Entity.project_id == project_id,
                Entity.entity_type == entity_type,
                Entity.entity_id == entity_id,
            )
        )
        if entity is None:
            entity = Entity(project_id=project_id, entity_type=entity_type, entity_id=entity_id)
            self.session.add(entity)
        entity.display_name = display_name
        entity.metadata_json = _json(metadata or {})
        entity.last_seen_at = _utc_now()
        self.session.flush()
        return entity


class JobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def enqueue(
        self,
        *,
        project_id: str,
        event_id: str | None,
        job_type: str,
        payload: dict[str, Any],
    ) -> Job:
        job = Job(project_id=project_id, event_id=event_id, job_type=job_type, payload_json=_json(payload))
        self.session.add(job)
        self.session.flush()
        return job

    def claim_next(self) -> Job | None:
        job = self.session.scalar(select(Job).where(Job.status == JobStatus.PENDING).order_by(Job.created_at))
        if job is None:
            return None
        job.status = JobStatus.RUNNING
        job.locked_at = _utc_now()
        job.attempt_count += 1
        self.session.flush()
        return job
```

- [ ] **Step 4: Run tests to verify pass**

Run:

```bash
python -m pytest tests/store/test_repositories.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run only during future implementation:

```bash
git add src/mem0_sidecar/store/repositories.py tests/store/test_repositories.py
git commit -m "feat: add control plane repositories"
```

## Task 6: Category And Event Core Services

**Files:**
- Create: `src/mem0_sidecar/core/categories.py`
- Create: `src/mem0_sidecar/core/events.py`
- Create: `tests/core/test_categories.py`
- Create: `tests/core/test_events.py`

**Interfaces:**
- Consumes: repository classes from Task 5
- Produces: `extract_category(metadata: dict[str, object], configured_names: set[str]) -> str | None`
- Produces: `EventService.record_successful_mutation(...) -> Event`

- [ ] **Step 1: Write failing service tests**

Create `tests/core/test_categories.py`:

```python
from mem0_sidecar.core.categories import extract_category


def test_extract_category_trusts_explicit_metadata_type() -> None:
    category = extract_category(
        metadata={"type": "decision"},
        configured_names={"decision", "task_learning"},
    )

    assert category == "decision"


def test_extract_category_ignores_unknown_values() -> None:
    category = extract_category(
        metadata={"category": "unknown"},
        configured_names={"decision"},
    )

    assert category is None
```

Create `tests/core/test_events.py`:

```python
from mem0_sidecar.core.events import EventService
from mem0_sidecar.store.models import EventStatus
from mem0_sidecar.store.repositories import EventRepository


def test_event_service_records_successful_mutation(db_session) -> None:
    service = EventService(EventRepository(db_session))

    event = service.record_successful_mutation(
        project_id="repo-a",
        operation="memory.add",
        subject_type="memory",
        subject_id="mem-1",
        request={"text": "Remember this"},
        response={"id": "mem-1"},
    )

    assert event.status is EventStatus.SUCCEEDED
    assert event.subject_id == "mem-1"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python -m pytest tests/core/test_categories.py tests/core/test_events.py -v
```

Expected: FAIL with missing core modules.

- [ ] **Step 3: Implement category extraction**

Create `src/mem0_sidecar/core/categories.py`:

```python
from collections.abc import Mapping


def extract_category(metadata: Mapping[str, object], configured_names: set[str]) -> str | None:
    candidates: list[object] = [
        metadata.get("type"),
        metadata.get("category"),
        metadata.get("custom_category"),
    ]
    categories_value = metadata.get("categories")
    if isinstance(categories_value, list):
        candidates.extend(categories_value)

    for candidate in candidates:
        if isinstance(candidate, str) and candidate in configured_names:
            return candidate
    return None
```

- [ ] **Step 4: Implement event service**

Create `src/mem0_sidecar/core/events.py`:

```python
from typing import Any

from mem0_sidecar.store.models import Event
from mem0_sidecar.store.repositories import EventRepository


class EventService:
    def __init__(self, events: EventRepository) -> None:
        self.events = events

    def record_successful_mutation(
        self,
        *,
        project_id: str,
        operation: str,
        subject_type: str,
        subject_id: str,
        request: dict[str, Any],
        response: dict[str, Any],
    ) -> Event:
        event = self.events.create_event(
            project_id=project_id,
            operation=operation,
            request=request,
            subject_type=subject_type,
            subject_id=subject_id,
        )
        return self.events.mark_succeeded(event.id, response=response)
```

- [ ] **Step 5: Run tests to verify pass**

Run:

```bash
python -m pytest tests/core/test_categories.py tests/core/test_events.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run only during future implementation:

```bash
git add src/mem0_sidecar/core/categories.py src/mem0_sidecar/core/events.py tests/core/test_categories.py tests/core/test_events.py
git commit -m "feat: add category and event services"
```

## Task 7: Mem0 OSS REST Client Boundary

**Files:**
- Create: `src/mem0_sidecar/mem0_client/__init__.py`
- Create: `src/mem0_sidecar/mem0_client/client.py`
- Create: `tests/mem0_client/test_client.py`

**Interfaces:**
- Consumes: `SidecarSettings.mem0_base_url`
- Produces: `Mem0RestClient`
- Produces: `Mem0RestClient.add_memory(payload: dict[str, object]) -> dict[str, object]`
- Produces: `Mem0RestClient.search_memories(payload: dict[str, object]) -> dict[str, object]`

- [ ] **Step 1: Write failing client tests**

Create `tests/mem0_client/test_client.py`:

```python
import httpx
import pytest

from mem0_sidecar.mem0_client.client import Mem0RestClient


@pytest.mark.asyncio
async def test_mem0_client_posts_add_memory_payload() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/memories/"
        assert request.headers["authorization"] == "Bearer local-key"
        assert request.read() == b'{"text":"hello"}'
        return httpx.Response(200, json={"id": "mem-1"})

    transport = httpx.MockTransport(handler)
    client = Mem0RestClient(
        base_url="http://mem0.local",
        api_key="local-key",
        transport=transport,
    )

    result = await client.add_memory({"text": "hello"})

    assert result == {"id": "mem-1"}
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python -m pytest tests/mem0_client/test_client.py -v
```

Expected: FAIL with missing client module.

- [ ] **Step 3: Implement Mem0 REST client**

Create `src/mem0_sidecar/mem0_client/__init__.py`:

```python
"""Mem0 OSS REST client boundary."""
```

Create `src/mem0_sidecar/mem0_client/client.py`:

```python
from typing import Any

import httpx


class Mem0RestClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.transport = transport

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        return {"Authorization": f"Bearer {self.api_key}"}

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers(),
            transport=self.transport,
            timeout=30.0,
        ) as client:
            response = await client.post(path, json=payload)
            response.raise_for_status()
            return dict(response.json())

    async def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/v1/memories/", payload)

    async def search_memories(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/v1/memories/search/", payload)
```

- [ ] **Step 4: Run tests to verify pass**

Run:

```bash
python -m pytest tests/mem0_client/test_client.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run only during future implementation:

```bash
git add src/mem0_sidecar/mem0_client tests/mem0_client/test_client.py
git commit -m "feat: add mem0 rest client boundary"
```

## Task 8: Worker Runner Skeleton

**Files:**
- Create: `src/mem0_sidecar/workers/__init__.py`
- Create: `src/mem0_sidecar/workers/runner.py`
- Create: `tests/workers/test_runner.py`

**Interfaces:**
- Consumes: `JobRepository.claim_next()`
- Produces: `WorkerRunner.run_once() -> bool`

- [ ] **Step 1: Write failing worker tests**

Create `tests/workers/test_runner.py`:

```python
from mem0_sidecar.store.models import JobStatus
from mem0_sidecar.store.repositories import JobRepository, ProjectRepository
from mem0_sidecar.workers.runner import WorkerRunner


def test_worker_runner_claims_one_pending_job(db_session) -> None:
    project = ProjectRepository(db_session).upsert_default_project(
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
    )
    job_repo = JobRepository(db_session)
    job = job_repo.enqueue(project_id=project.id, event_id=None, job_type="entity.rebuild", payload={})
    db_session.commit()

    did_work = WorkerRunner(job_repo).run_once()
    db_session.commit()

    assert did_work is True
    assert db_session.get(type(job), job.id).status is JobStatus.RUNNING
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python -m pytest tests/workers/test_runner.py -v
```

Expected: FAIL with missing worker module.

- [ ] **Step 3: Implement worker runner**

Create `src/mem0_sidecar/workers/__init__.py`:

```python
"""Background worker support."""
```

Create `src/mem0_sidecar/workers/runner.py`:

```python
from mem0_sidecar.store.repositories import JobRepository


class WorkerRunner:
    def __init__(self, jobs: JobRepository) -> None:
        self.jobs = jobs

    def run_once(self) -> bool:
        job = self.jobs.claim_next()
        return job is not None
```

- [ ] **Step 4: Run tests to verify pass**

Run:

```bash
python -m pytest tests/workers/test_runner.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run only during future implementation:

```bash
git add src/mem0_sidecar/workers tests/workers/test_runner.py
git commit -m "feat: add worker runner skeleton"
```

## Task 9: Minimal FastAPI App And Health Route

**Files:**
- Create: `src/mem0_sidecar/http_adapter/__init__.py`
- Create: `src/mem0_sidecar/http_adapter/app.py`
- Create: `tests/http_adapter/test_health.py`

**Interfaces:**
- Consumes: `load_settings()`
- Produces: `create_app() -> fastapi.FastAPI`
- Produces: `GET /healthz`

- [ ] **Step 1: Write failing health route test**

Create `tests/http_adapter/test_health.py`:

```python
from fastapi.testclient import TestClient

from mem0_sidecar.http_adapter.app import create_app


def test_healthz_reports_sidecar_status() -> None:
    client = TestClient(create_app())

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "mem0-platform-sidecar"}
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python -m pytest tests/http_adapter/test_health.py -v
```

Expected: FAIL with missing HTTP adapter module.

- [ ] **Step 3: Implement app factory**

Create `src/mem0_sidecar/http_adapter/__init__.py`:

```python
"""HTTP adapter package."""
```

Create `src/mem0_sidecar/http_adapter/app.py`:

```python
from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="Mem0 Platform Sidecar")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "mem0-platform-sidecar"}

    return app
```

- [ ] **Step 4: Run tests to verify pass**

Run:

```bash
python -m pytest tests/http_adapter/test_health.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run only during future implementation:

```bash
git add src/mem0_sidecar/http_adapter tests/http_adapter/test_health.py
git commit -m "feat: add health route"
```

## Task 10: Control-Plane Integration Flow

**Files:**
- Create: `tests/integration/test_control_plane_flow.py`
- Modify: `src/mem0_sidecar/core/projects.py`

**Interfaces:**
- Consumes: repositories from Task 5
- Consumes: `normalize_scope(...)`
- Consumes: `extract_category(...)`
- Consumes: `EventService.record_successful_mutation(...)`
- Produces: `bootstrap_project(...) -> Project`

- [ ] **Step 1: Write failing integration test**

Create `tests/integration/test_control_plane_flow.py`:

```python
from mem0_sidecar.core.categories import extract_category
from mem0_sidecar.core.events import EventService
from mem0_sidecar.core.projects import bootstrap_project
from mem0_sidecar.core.scope import normalize_scope
from mem0_sidecar.store.models import EventStatus
from mem0_sidecar.store.repositories import (
    CategoryRepository,
    EntityRepository,
    EventRepository,
    MemoryIndexRepository,
)


def test_control_plane_core_indexes_memory_with_category_event_and_entity(db_session) -> None:
    project = bootstrap_project(
        db_session,
        project_id="repo-a",
        name="Repo A",
        mem0_base_url="http://mem0:8000",
        default_user_id="root",
        default_agent_id="codex",
    )
    CategoryRepository(db_session).replace_project_categories(
        project_id=project.id,
        categories=[{"name": "decision", "description": "Architecture decisions"}],
    )
    scope = normalize_scope(
        project_id=project.id,
        user_id="root",
        app_id=None,
        agent_id="codex",
        run_id="session-1",
    )
    category = extract_category({"type": "decision"}, {"decision"})
    memory = MemoryIndexRepository(db_session).upsert_memory(
        project_id=project.id,
        mem0_memory_id="mem-1",
        user_id=scope.user_id,
        app_id=scope.app_id,
        agent_id=scope.agent_id,
        run_id=scope.run_id,
        category=category,
        metadata={"type": "decision"},
    )
    entity = EntityRepository(db_session).upsert_entity(
        project_id=project.id,
        entity_type="app",
        entity_id=scope.app_id,
        display_name=scope.app_id,
    )
    event = EventService(EventRepository(db_session)).record_successful_mutation(
        project_id=project.id,
        operation="memory.add",
        subject_type="memory",
        subject_id=memory.mem0_memory_id,
        request={"metadata": {"type": "decision"}},
        response={"id": memory.mem0_memory_id},
    )
    db_session.commit()

    assert memory.category == "decision"
    assert entity.entity_id == "repo-a"
    assert event.status is EventStatus.SUCCEEDED
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python -m pytest tests/integration/test_control_plane_flow.py -v
```

Expected: FAIL with missing `bootstrap_project`.

- [ ] **Step 3: Implement project bootstrap service**

Create `src/mem0_sidecar/core/projects.py`:

```python
from sqlalchemy.orm import Session

from mem0_sidecar.store.models import Project
from mem0_sidecar.store.repositories import ProjectRepository


def bootstrap_project(
    session: Session,
    *,
    project_id: str,
    name: str,
    mem0_base_url: str,
    default_user_id: str | None = None,
    default_agent_id: str | None = None,
) -> Project:
    return ProjectRepository(session).upsert_default_project(
        project_id=project_id,
        name=name,
        mem0_base_url=mem0_base_url,
        default_user_id=default_user_id,
        default_agent_id=default_agent_id,
    )
```

- [ ] **Step 4: Run integration test to verify pass**

Run:

```bash
python -m pytest tests/integration/test_control_plane_flow.py -v
```

Expected: PASS.

- [ ] **Step 5: Run the full test suite**

Run:

```bash
python -m pytest -v
```

Expected: PASS for all tests created in this plan.

- [ ] **Step 6: Commit**

Run only during future implementation:

```bash
git add src/mem0_sidecar/core/projects.py tests/integration/test_control_plane_flow.py
git commit -m "test: cover control plane core flow"
```

## Task 11: Development Docs And Local Compose Skeleton

**Files:**
- Create: `docs/development.md`
- Create: `docker/Dockerfile`
- Create: `docker/docker-compose.dev.yml`

**Interfaces:**
- Consumes: settings from Task 2
- Produces: local command reference for installing, testing, and running health route
- Produces: Dockerfile and Docker Compose service definition for the sidecar container

- [ ] **Step 1: Write development guide**

Create `docs/development.md`:

````markdown
# Development

## Install

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Test

```bash
python -m pytest -v
```

## Run HTTP Health Route

```bash
uvicorn mem0_sidecar.http_adapter.app:create_app --factory --host 127.0.0.1 --port 8765
curl http://127.0.0.1:8765/healthz
```

Expected response:

```json
{"status":"ok","service":"mem0-platform-sidecar"}
```

## Configuration

The service reads these environment variables:

- `MEM0_SIDECAR_DATABASE_URL`
- `MEM0_SIDECAR_MEM0_BASE_URL`
- `MEM0_SIDECAR_MEM0_API_KEY`
- `MEM0_SIDECAR_DEFAULT_PROJECT_ID`
- `MEM0_SIDECAR_WORKER_POLL_INTERVAL_SECONDS`
````

- [ ] **Step 2: Write Dockerfile and Compose skeleton**

Create `docker/Dockerfile`:

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir -e .

EXPOSE 8765

CMD ["uvicorn", "mem0_sidecar.http_adapter.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765"]
```

Create `docker/docker-compose.dev.yml`:

```yaml
services:
  sidecar:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    environment:
      MEM0_SIDECAR_DATABASE_URL: sqlite:////data/mem0_sidecar.sqlite3
      MEM0_SIDECAR_MEM0_BASE_URL: http://mem0:8000
      MEM0_SIDECAR_DEFAULT_PROJECT_ID: default
    volumes:
      - sidecar-data:/data
    ports:
      - "8765:8765"
    command:
      - uvicorn
      - mem0_sidecar.http_adapter.app:create_app
      - --factory
      - --host
      - 0.0.0.0
      - --port
      - "8765"

volumes:
  sidecar-data:
```

- [ ] **Step 3: Run documentation checks by executing referenced commands**

Run:

```bash
python -m pytest -v
```

Expected: PASS.

Run:

```bash
python -m uvicorn mem0_sidecar.http_adapter.app:create_app --factory --host 127.0.0.1 --port 8765
```

Expected: server starts and logs `Uvicorn running on http://127.0.0.1:8765`.

Stop the server with `Ctrl-C`.

- [ ] **Step 4: Commit**

Run only during future implementation:

```bash
git add docs/development.md docker/Dockerfile docker/docker-compose.dev.yml
git commit -m "docs: add development workflow"
```

## Final Verification

- [ ] **Step 1: Run the full test suite**

Run:

```bash
cd /workspace/data/mem0/mem0-platform-sidecar
python -m pytest -v
```

Expected: PASS.

- [ ] **Step 2: Run lint**

Run:

```bash
python -m ruff check .
```

Expected: PASS.

- [ ] **Step 3: Confirm the sidecar did not modify Mem0 OSS**

Run:

```bash
git -C /workspace/data/mem0/mem0-oss-mcp status --short --branch
```

Expected: no changes created by this implementation plan. Existing unrelated untracked files can remain untouched.

- [ ] **Step 4: Confirm Phase 0-1 acceptance**

Check:

- package imports;
- settings load from defaults and environment variables;
- SQLite models persist all Phase 1 control-plane objects;
- repositories create and update projects, categories, events, memory indexes, entities, and jobs;
- scope normalization preserves `app_id`;
- category extraction uses explicit metadata;
- worker runner can claim a pending job;
- health route responds;
- integration test covers category, memory index, entity, and event together.
