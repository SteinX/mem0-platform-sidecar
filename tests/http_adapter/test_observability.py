from fastapi.testclient import TestClient

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.http_adapter.app import create_app


def test_request_logging_sets_request_id_header_and_structured_record(
    tmp_path,
    caplog,
) -> None:
    app = create_app(
        settings=SidecarSettings(
            database_url=f"sqlite:///{tmp_path / 'sidecar.sqlite3'}",
            mem0_base_url="http://mem0.local",
            request_id_header="X-Correlation-ID",
        )
    )
    client = TestClient(app)

    with caplog.at_level("INFO", logger="mem0_sidecar.http"):
        response = client.get(
            "/healthz",
            headers={"X-Correlation-ID": "req-123"},
        )

    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == "req-123"
    assert any(
        record.message == "http_request_completed"
        and record.request_id == "req-123"
        and record.method == "GET"
        and record.path == "/healthz"
        and record.status_code == 200
        and record.duration_ms >= 0
        for record in caplog.records
    )
