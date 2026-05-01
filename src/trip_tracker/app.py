"""FastAPI app factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from trip_tracker import __version__
from trip_tracker.auth.routes import router as auth_router
from trip_tracker.config import Settings
from trip_tracker.db import dispose_db, init_db
from trip_tracker.ingest.webhook import router as ingest_router
from trip_tracker.logging_setup import configure_logging
from trip_tracker.routes.admin import router as admin_router
from trip_tracker.routes.documents import router as documents_router
from trip_tracker.routes.health import router as health_router
from trip_tracker.routes.home import router as home_router
from trip_tracker.routes.ics import router as ics_router
from trip_tracker.routes.inbox import router as inbox_router
from trip_tracker.routes.segments import router as segments_router
from trip_tracker.routes.trips import router as trips_router
from trip_tracker.search.client import build_client, ensure_indexes_configured
from trip_tracker.search.proxy import router as search_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    init_db(settings)
    app.state.meili = build_client(settings)
    try:
        await ensure_indexes_configured(app.state.meili)
    except Exception as exc:
        # Don't fail the app if Meili is down at boot — search is non-critical.
        logger.warning("Meili index config failed at startup: %s", exc)
    try:
        yield
    finally:
        await dispose_db()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(level=settings.log_level, format=settings.log_format)

    app = FastAPI(
        title="trip-tracker",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,  # no public Swagger
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings

    # Static files (Tailwind output ships baked into the image).
    # Use a path computed from __file__ so the app works regardless of CWD.
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir), check_dir=False), name="static")

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(ingest_router)
    app.include_router(home_router)
    app.include_router(trips_router)
    app.include_router(segments_router)
    app.include_router(inbox_router)
    app.include_router(admin_router)
    app.include_router(search_router)
    app.include_router(documents_router)
    app.include_router(ics_router)

    return app
