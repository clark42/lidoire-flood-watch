"""Main FastAPI application."""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.api import router as api_router
from app.services.scheduler import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    settings = get_settings()
    print(f"Starting Lidoire Flood Watch API...")
    print(f"Moulin location: {settings.moulin_lat}, {settings.moulin_lon}")

    # Start background data collection
    await start_scheduler()

    yield

    # Shutdown
    await stop_scheduler()
    print("Lidoire Flood Watch API stopped.")


app = FastAPI(
    title="Lidoire Flood Watch",
    description="API de surveillance des inondations de la Lidoire au moulin",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url, "http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routes
app.include_router(api_router, prefix="/api")


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": "Lidoire Flood Watch",
        "version": "1.0.0",
        "status": "running",
    }
