"""
Combined server entry point — runs the webhook receiver AND
an optional background calling loop in the same process.

For production, you'd run these as separate processes. This is
convenient for the MVP / dev.

Usage:
    python -m app.server
    # or
    uvicorn app.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import structlog
import uvicorn
from fastapi import FastAPI

from app.config import get_settings
from app.database import Database
from app.logging_config import setup_logging
from app.webhook import create_webhook_app

log = structlog.get_logger(__name__)

settings = get_settings()
setup_logging(settings.log_dir, json_logs=True)
settings.ensure_dirs()

# Global database instance (shared between webhook & caller)
db = Database(settings.database_path)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hooks."""
    await db.connect()
    log.info("database_connected", path=str(settings.database_path))
    yield
    await db.close()
    log.info("database_closed")


# Create the main app
app = create_webhook_app(settings, db)
app.router.lifespan_context = lifespan


if __name__ == "__main__":
    uvicorn.run(
        "app.server:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )
