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
