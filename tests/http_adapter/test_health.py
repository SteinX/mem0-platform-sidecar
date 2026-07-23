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


def test_consolidation_lifespan_starts_named_scheduler_and_worker(tmp_path) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            consolidation_enabled=True,
        ),
        mem0_client=object(),
    )

    with TestClient(app):
        scheduler_task = app.state.consolidation_scheduler_task
        worker_task = app.state.consolidation_worker_task
        assert scheduler_task.get_name() == "mem0-consolidation-scheduler"
        assert worker_task.get_name() == "mem0-consolidation-worker"
        assert not scheduler_task.done()
        assert not worker_task.done()

    assert scheduler_task.done()
    assert worker_task.done()


def test_bridge_routing_heartbeat_is_persisted_for_consolidation_status(
    tmp_path,
) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
        )
    )

    with TestClient(app) as client:
        heartbeat = client.post(
            "/v1/projects/default/capabilities/bridge-routing/heartbeat",
            json={
                "instance_id": "bridge-a",
                "bridge_version": "0.1.1",
                "routes_reads": True,
                "routes_writes": True,
            },
        )
        status = client.get(
            "/v1/projects/default/apps/default/consolidation"
        )

    assert heartbeat.status_code == 200
    assert heartbeat.json()["ready"] is True
    assert status.status_code == 200
    assert status.json()["bridge_routing_ready"] is True
    assert "worker" in status.json()
