"""
Combined server entry point — runs the webhook receiver.

Usage:
    python -m app.server
    # or
    uvicorn app.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.webhook import create_webhook_app

log = structlog.get_logger(__name__)


def create_app() -> FastAPI:
    """Create the FastAPI app with minimal dependencies."""
    
    app = FastAPI(
        title="AI Recruitment Caller — Webhook Receiver",
        version="0.1.0",
    )
    
    # ── Health check (no deps) ────────────────────────────────
    @app.get("/health")
    async def health():
        return {"status": "ok"}
    
    @app.get("/")
    async def root():
        return {"message": "AI Recruitment Caller API"}
    
    return app


# Create the main app
app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
