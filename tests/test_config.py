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
