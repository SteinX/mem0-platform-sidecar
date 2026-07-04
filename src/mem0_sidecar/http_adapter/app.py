from fastapi import FastAPI, HTTPException
from sqlalchemy import text

from mem0_sidecar.config import SidecarSettings, load_settings
from mem0_sidecar.http_adapter.event_routes import event_router
from mem0_sidecar.http_adapter.memory_routes import memory_router
from mem0_sidecar.mem0_client.client import Mem0RestClient
from mem0_sidecar.store.database import create_engine_from_url, create_session_factory
from mem0_sidecar.store.models import Base
from mem0_sidecar.store.repositories import ProjectRepository


def create_app(
    settings: SidecarSettings | None = None,
    *,
    session_factory=None,
    mem0_client=None,
) -> FastAPI:
    settings = settings or load_settings()

    if session_factory is None:
        engine = create_engine_from_url(settings.database_url)
        Base.metadata.create_all(engine)
        session_factory = create_session_factory(engine)

    with session_factory() as session:
        ProjectRepository(session).upsert_default_project(
            project_id=settings.default_project_id,
            name=settings.default_project_id,
            mem0_base_url=settings.mem0_base_url,
        )
        session.commit()

    if mem0_client is None:
        mem0_client = Mem0RestClient(
            base_url=settings.mem0_base_url,
            api_key=settings.mem0_api_key,
        )

    app = FastAPI(title="Mem0 Platform Sidecar")
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.mem0_client = mem0_client
    app.include_router(memory_router)
    app.include_router(event_router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "mem0-platform-sidecar"}

    @app.get("/readyz")
    def readyz() -> dict[str, str]:
        try:
            with app.state.session_factory() as session:
                session.execute(text("SELECT 1"))
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "status": "error",
                    "service": "mem0-platform-sidecar",
                    "database": "unavailable",
                },
            ) from exc

        return {
            "status": "ok",
            "service": "mem0-platform-sidecar",
            "database": "ok",
        }

    return app
