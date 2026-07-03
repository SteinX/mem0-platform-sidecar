from fastapi import FastAPI

from mem0_sidecar.config import SidecarSettings, load_settings


def create_app(settings: SidecarSettings | None = None) -> FastAPI:
    settings = settings or load_settings()

    app = FastAPI(title="Mem0 Platform Sidecar")
    app.state.settings = settings

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "mem0-platform-sidecar"}

    return app
