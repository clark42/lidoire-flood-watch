"""Main FastAPI application."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import Settings, get_settings
from app.api import router as api_router
from app.api.deps import API_KEY_HEADER_NAME, warn_if_api_key_missing
from app.services.scheduler import start_scheduler, stop_scheduler
from app.db import init_db, close_db
from app.services.cache import close_redis

# Application loggers (uvicorn configures only its own "uvicorn.*" loggers).
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

# The API is consumed same-origin through the nginx /api proxy (or the Vite dev
# proxy); CORS only matters for direct cross-origin calls during development.
CORS_ALLOW_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
CORS_ALLOW_HEADERS = ["Content-Type", "Accept", API_KEY_HEADER_NAME]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    settings = get_settings()
    logger.info("Starting Lidoire Flood Watch API...")
    logger.info("Moulin location: %s, %s", settings.moulin_lat, settings.moulin_lon)
    warn_if_api_key_missing(settings)

    # Create PostgreSQL tables
    await init_db()

    # Start background data collection
    await start_scheduler()

    yield

    # Shutdown
    await stop_scheduler()
    await close_redis()
    await close_db()
    logger.info("Lidoire Flood Watch API stopped.")


def configure_cors(app: FastAPI, settings: Settings) -> list[str]:
    """Install the CORS middleware with an explicit origin allowlist.

    No wildcard and no credentials: authentication uses the X-API-Key header,
    not cookies. Returns the allowed origins.
    """
    origins = settings.cors_origin_list
    if "*" in [o.strip() for o in settings.cors_origins.split(",")]:
        logger.warning("CORS_ORIGINS contains '*': ignored, list explicit origins instead")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=CORS_ALLOW_METHODS,
        allow_headers=CORS_ALLOW_HEADERS,
        expose_headers=["X-Backfill-Pending"],
    )
    return origins


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application."""
    settings = settings or get_settings()

    application = FastAPI(
        title="Lidoire Flood Watch",
        description="API de surveillance des inondations de la Lidoire au moulin",
        version="1.0.0",
        lifespan=lifespan,
    )

    configure_cors(application, settings)

    # Include API routes (write methods protected by the X-API-Key dependency)
    application.include_router(api_router, prefix="/api")

    @application.get("/")
    async def root():
        """Root endpoint."""
        return {
            "name": "Lidoire Flood Watch",
            "version": "1.0.0",
            "status": "running",
        }

    return application


app = create_app()
