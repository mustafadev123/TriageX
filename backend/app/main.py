"""TriageX backend entrypoint."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api.health import router as health_router
from app.api.reference import router as reference_router
from app.api.runs import router as runs_router
from app.api.ws import ConnectionManager
from app.api.ws import router as ws_router
from app.config import APP_NAME, get_settings
from app.logging_config import configure_logging
from app.services.demo_service import DemoService


def create_app(demo_service: DemoService | None = None) -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title=f"{APP_NAME} API", version="0.3.0")
    app.state.demo_service = demo_service or DemoService()
    app.state.ws_manager = ConnectionManager()
    app.add_middleware(GZipMiddleware, minimum_size=2048)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health_router, prefix="/api")
    app.include_router(reference_router, prefix="/api")
    app.include_router(runs_router, prefix="/api")
    app.include_router(ws_router)
    return app


app = create_app()
