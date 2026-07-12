from mem0_sidecar.config import SidecarSettings, load_settings


def test_settings_defaults_use_local_development_values(monkeypatch) -> None:
    monkeypatch.delenv("MEM0_SIDECAR_DATABASE_URL", raising=False)
    monkeypatch.delenv("MEM0_SIDECAR_MEM0_BASE_URL", raising=False)

    settings = load_settings()

    assert settings.database_url == "sqlite:///./mem0_sidecar.sqlite3"
    assert settings.mem0_base_url == "http://127.0.0.1:8000"
    assert settings.default_project_id == "default"
    assert settings.allow_adopt_unscoped_memories is False


def test_settings_can_be_loaded_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("MEM0_SIDECAR_DATABASE_URL", "sqlite:///tmp/test.sqlite3")
    monkeypatch.setenv("MEM0_SIDECAR_MEM0_BASE_URL", "http://mem0:8000")
    monkeypatch.setenv("MEM0_SIDECAR_DEFAULT_PROJECT_ID", "repo-a")
    monkeypatch.setenv("MEM0_SIDECAR_MEM0_API_KEY_HEADER_NAME", "Authorization")
    monkeypatch.setenv("MEM0_SIDECAR_MEM0_API_KEY_PREFIX", "Bearer")
    monkeypatch.setenv(
        "MEM0_SIDECAR_MEM0_EXTRA_HEADERS",
        '{"X-Mem0-Org":"local","X-Trace-Source":"sidecar"}',
    )
    monkeypatch.setenv("MEM0_SIDECAR_MEM0_REQUEST_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("MEM0_SIDECAR_MEM0_CONNECT_TIMEOUT_SECONDS", "3.5")
    monkeypatch.setenv("MEM0_SIDECAR_MEM0_VERIFY_TLS", "false")
    monkeypatch.setenv("MEM0_SIDECAR_MEM0_CA_BUNDLE", "/etc/ssl/certs/mem0.pem")
    monkeypatch.setenv("MEM0_SIDECAR_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("MEM0_SIDECAR_LOG_FORMAT", "json")
    monkeypatch.setenv("MEM0_SIDECAR_REQUEST_ID_HEADER", "X-Correlation-ID")
    monkeypatch.setenv("MEM0_SIDECAR_ALLOW_ADOPT_UNSCOPED", "true")

    settings = SidecarSettings()

    assert settings.database_url == "sqlite:///tmp/test.sqlite3"
    assert settings.mem0_base_url == "http://mem0:8000"
    assert settings.default_project_id == "repo-a"
    assert settings.mem0_api_key_header_name == "Authorization"
    assert settings.mem0_api_key_prefix == "Bearer"
    assert settings.mem0_extra_headers == {
        "X-Mem0-Org": "local",
        "X-Trace-Source": "sidecar",
    }
    assert settings.mem0_request_timeout_seconds == 12.5
    assert settings.mem0_connect_timeout_seconds == 3.5
    assert settings.mem0_verify_tls is False
    assert settings.mem0_ca_bundle == "/etc/ssl/certs/mem0.pem"
    assert settings.log_level == "DEBUG"
    assert settings.log_format == "json"
    assert settings.request_id_header == "X-Correlation-ID"
    assert settings.allow_adopt_unscoped_memories is True
