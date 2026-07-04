from fastapi.testclient import TestClient
from sqlalchemy import select

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app
from mem0_sidecar.store.models import Project


def test_healthz_reports_sidecar_status() -> None:
    client = TestClient(create_app())

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "mem0-platform-sidecar"}


def test_readyz_checks_database_session(tmp_path) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
        )
    )
    client = TestClient(app)

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "mem0-platform-sidecar",
        "database": "ok",
    }


def test_readyz_returns_503_when_database_session_fails(tmp_path) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
        )
    )

    def failing_session_factory():
        raise RuntimeError("database unavailable")

    app.state.session_factory = failing_session_factory
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "status": "error",
            "service": "mem0-platform-sidecar",
            "database": "unavailable",
        }
    }


def test_create_app_stores_injected_settings_and_clients(tmp_path) -> None:
    settings = SidecarSettings(
        database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
        mem0_base_url="http://mem0.local",
    )
    mem0_client = object()
    app = create_app(settings=settings, mem0_client=mem0_client)

    assert app.state.settings is settings
    assert app.state.mem0_client is mem0_client
    assert app.state.session_factory is not None


def test_create_app_bootstraps_default_project(tmp_path) -> None:
    settings = SidecarSettings(
        database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
        mem0_base_url="http://mem0.local",
    )
    app = create_app(settings=settings)

    with app.state.session_factory() as session:
        project = session.scalar(
            select(Project).where(Project.id == settings.default_project_id)
        )

    assert project is not None
    assert project.name == settings.default_project_id
    assert project.mem0_base_url == settings.mem0_base_url


def test_create_app_configures_mem0_client_from_settings(tmp_path) -> None:
    settings = SidecarSettings(
        database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
        mem0_base_url="https://mem0.example/api",
        mem0_api_key="token",
        mem0_api_key_header_name="Authorization",
        mem0_api_key_prefix="Bearer",
        mem0_extra_headers={"X-Mem0-Org": "org-1"},
        mem0_request_timeout_seconds=14.0,
        mem0_connect_timeout_seconds=2.0,
        mem0_verify_tls=False,
    )

    app = create_app(settings=settings)

    mem0 = app.state.mem0_client
    assert mem0.base_url == "https://mem0.example/api"
    assert mem0.api_key == "token"
    assert mem0.api_key_header_name == "Authorization"
    assert mem0.api_key_prefix == "Bearer"
    assert mem0.extra_headers == {"X-Mem0-Org": "org-1"}
    assert mem0.request_timeout_seconds == 14.0
    assert mem0.connect_timeout_seconds == 2.0
    assert mem0.verify_tls is False
