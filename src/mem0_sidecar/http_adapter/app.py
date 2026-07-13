from fastapi import FastAPI, HTTPException
from sqlalchemy import text

from mem0_sidecar.config import SidecarSettings, load_settings
from mem0_sidecar.http_adapter.category_routes import category_router
from mem0_sidecar.http_adapter.entity_routes import entity_router
from mem0_sidecar.http_adapter.event_routes import event_router
from mem0_sidecar.http_adapter.export_routes import export_router
from mem0_sidecar.http_adapter.memory_routes import memory_router
from mem0_sidecar.mem0_client.client import Mem0RestClient
from mem0_sidecar.observability import RequestLoggingMiddleware, configure_logging
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
    configure_logging(settings)

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
            api_key_header_name=settings.mem0_api_key_header_name,
            api_key_prefix=settings.mem0_api_key_prefix,
            extra_headers=settings.mem0_extra_headers,
            request_timeout_seconds=settings.mem0_request_timeout_seconds,
            connect_timeout_seconds=settings.mem0_connect_timeout_seconds,
            verify_tls=settings.mem0_verify_tls,
            ca_bundle=settings.mem0_ca_bundle,
            memories_path=settings.mem0_memories_path,
            search_path=settings.mem0_search_path,
        )

    app = FastAPI(title="Mem0 Platform Sidecar")
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.mem0_client = mem0_client
    app.add_middleware(
        RequestLoggingMiddleware,
        request_id_header=settings.request_id_header,
    )
    app.include_router(memory_router)
    app.include_router(event_router)
    app.include_router(entity_router)
    app.include_router(category_router)
    app.include_router(export_router)

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
