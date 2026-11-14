"""TriageX backend entrypoint."""

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api.chert import router as chert_router
from app.api.db import router as db_router
from app.api.health import router as health_router
from app.api.meta import router as meta_router
from app.api.reference import router as reference_router
from app.api.runs import router as runs_router
from app.api.ws import ConnectionManager
from app.api.ws import router as ws_router
from app.config import APP_NAME, get_settings
from app.logging_config import configure_logging
from app.repositories.db import Database
from app.repositories.persistence import PersistenceWriter
from app.services.demo_service import DemoService
from app.services.outreach import ChertConfig, OutreachService

log = logging.getLogger("triagex")


def writer_from_settings() -> PersistenceWriter:
    settings = get_settings()
    if not settings.database_url:
        return PersistenceWriter(None)
    db = Database(
        settings.database_url,
        schema=settings.database_schema,
        pool_size=settings.database_pool_size,
        timeout_seconds=settings.database_timeout_seconds,
    )
    log.info("database configured", extra={"target": db.redacted_url})
    return PersistenceWriter(db)


def create_app(
    demo_service: DemoService | None = None,
    persistence: PersistenceWriter | None = None,
    outreach: OutreachService | None = None,
) -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    owns_writer = persistence is None
    writer = writer_from_settings() if owns_writer else persistence
    demo = outreach.demo if outreach is not None else (demo_service or DemoService())
    if outreach is None:
        outreach = OutreachService(demo, ChertConfig.from_settings(settings))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        writer.start()  # connects and migrates in the background; never blocks startup
        if outreach.client is not None:  # ask Chert whether the credentials work
            threading.Thread(
                target=outreach.check_connection, name="triagex-chert-check", daemon=True
            ).start()
        yield
        if owns_writer:  # an injected writer (tests, scripts) is managed by its owner
            writer.stop()
            if writer.db is not None:
                writer.db.close()

    app = FastAPI(title=f"{APP_NAME} API", version="0.4.0", lifespan=lifespan)
    app.state.demo_service = demo
    app.state.outreach = outreach
    app.state.background_tasks = set()
    app.state.persistence = writer
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
    app.include_router(db_router, prefix="/api")
    app.include_router(meta_router, prefix="/api")
    app.include_router(chert_router, prefix="/api")
    app.include_router(ws_router)
    return app


app = create_app()
