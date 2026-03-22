"""
Combined server entry point — runs the webhook receiver.

Usage:
    python -m app.server
    # or
    uvicorn app.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import structlog
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.csv_pipeline import ingest_csv
from app.database import Database
from app.models import CallRecord, Disposition
from app.output import generate_output_csv, generate_rejected_csv, generate_run_summary
from app.scheduler import CallScheduler
from app.twilio_service import TwilioService
from app.media_stream import handle_media_stream
from app.webhook import (
    handle_twilio_voice,
    handle_twilio_status,
)

log = structlog.get_logger(__name__)

# Module-level references populated during lifespan
_db: Optional[Database] = None
_settings = None
_twilio: Optional[TwilioService] = None
_active_call_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start up: connect DB, init Twilio. Shut down: close both."""
    global _db, _settings, _twilio
    _settings = get_settings()
    _settings.ensure_dirs()
    _db = Database(_settings.database_path)
    await _db.connect()
    _twilio = TwilioService(_settings)
    log.info("server_started", db=str(_settings.database_path))
    yield
    if _twilio:
        await _twilio.close()
    await _db.close()
    log.info("server_stopped")


def create_app() -> FastAPI:
    """Create the FastAPI app with all routes."""

    app = FastAPI(
        title="AI Recruitment Caller",
        version="0.2.0",
        lifespan=lifespan,
    )

    # ── Health check ──────────────────────────────────────────
    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # ── Serve the dashboard UI ─────────────────────────────────
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def root():
        index_path = static_dir / "index.html"
        if index_path.exists():
            return HTMLResponse(content=index_path.read_text(), status_code=200)
        return HTMLResponse(content="<h1>AI Recruitment Caller API v0.2.0</h1>", status_code=200)

    # ─────────────────────────────────────────────────────────
    #   Test Call (Debug) Endpoint
    # ─────────────────────────────────────────────────────────
    @app.post("/test-call")
    async def test_call(phone: str = Form(...), name: str = Form(default="Test")):
        """
        Immediately attempt one outbound call (synchronous) and return
        the Twilio response or error — useful for debugging.
        """
        if not _settings.twilio_account_sid:
            return {"error": "No Twilio credentials — set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN."}
        try:
            result = await _twilio.place_call(
                phone_e164=phone,
                from_number=_settings.twilio_phone_number,
                candidate_name=name,
                record_id="test-debug",
                job_role="Test Call",
            )
            return {"status": "ok", "twilio_response": result}
        except Exception as e:
            return {"status": "error", "error": str(e), "type": type(e).__name__}

    # ─────────────────────────────────────────────────────────
    #   CSV Upload Endpoint
    # ─────────────────────────────────────────────────────────
    @app.post("/upload-csv")
    async def upload_csv(
        file: UploadFile = File(...),
        job_role: str = Form(default=""),
    ):
        """
        Upload a candidate CSV file with columns: unique_record_id, first_name, phone
        Optionally specify a job_role to qualify candidates against.
        """
        if not file.filename or not file.filename.endswith(".csv"):
            raise HTTPException(status_code=400, detail="File must be a .csv")

        # Save uploaded file
        upload_dir = _settings.input_csv_dir
        upload_dir.mkdir(parents=True, exist_ok=True)
        dest = upload_dir / f"upload_{uuid.uuid4().hex[:8]}_{file.filename}"

        content = await file.read()
        with open(dest, "wb") as f:
            f.write(content)

        # Ingest
        try:
            suppression_path = (
                _settings.suppression_list_path
                if _settings.suppression_list_path.exists()
                else None
            )
            valid, rejected = ingest_csv(dest, suppression_path)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # Persist valid candidates
        for candidate in valid:
            record = CallRecord(
                unique_record_id=candidate.unique_record_id,
                first_name=candidate.first_name,
                last_name=candidate.last_name,
                phone_e164=candidate.phone_e164,
                job_role=job_role,
                status=Disposition.PENDING,
            )
            await _db.upsert_candidate(record)

        # Write rejected rows
        if rejected:
            await generate_rejected_csv(rejected, _settings.output_csv_dir)

        log.info(
            "csv_uploaded",
            filename=file.filename,
            valid=len(valid),
            rejected=len(rejected),
            job_role=job_role,
        )

        # Build rejection summary for the response
        rejection_details = []
        for r in rejected[:20]:  # Show first 20 rejections max
            rejection_details.append({
                "row": r.get("_row", "?"),
                "reason": r.get("_reason", "unknown"),
                "phone": r.get("phone", r.get("Phone", "")),
            })

        return {
            "status": "ok",
            "valid_records": len(valid),
            "rejected_records": len(rejected),
            "job_role": job_role or "(none — generic screening)",
            "rejected_details": rejection_details,
            "message": f"Ingested {len(valid)} candidates. Use POST /start-calls to begin calling.",
        }

    # ─────────────────────────────────────────────────────────
    #   Start Calls Endpoint
    # ─────────────────────────────────────────────────────────
    @app.post("/start-calls")
    async def start_calls():
        """
        Begin placing outbound AI calls to all pending candidates.
        Runs asynchronously in the background.
        """
        global _active_call_task

        if not _settings.twilio_account_sid:
            raise HTTPException(
                status_code=503,
                detail="Twilio not configured. Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_PHONE_NUMBER.",
            )

        if _active_call_task and not _active_call_task.done():
            return {"status": "already_running", "message": "A calling batch is already in progress."}

        run_id = uuid.uuid4().hex[:12]

        async def _run_calls():
            scheduler = CallScheduler(_settings, _db, _twilio, run_id)
            try:
                stats = await scheduler.run_batch()
                log.info("api_call_batch_complete", run_id=run_id, **stats)
            except Exception as e:
                log.error("api_call_batch_error", run_id=run_id, error=str(e))

        _active_call_task = asyncio.create_task(_run_calls())

        # Check pending count
        pending = await _db.get_pending_records(limit=1)
        pending_count = len(pending)

        return {
            "status": "started",
            "run_id": run_id,
            "message": f"Calling batch started. {pending_count}+ pending candidates in queue.",
        }

    # ─────────────────────────────────────────────────────────
    #   Status Endpoint
    # ─────────────────────────────────────────────────────────
    @app.get("/status")
    async def status():
        """Get current pipeline status and statistics."""
        summary = await generate_run_summary(_db)
        calling_active = _active_call_task and not _active_call_task.done()
        return {
            "calling_active": calling_active,
            **summary,
        }

    # ─────────────────────────────────────────────────────────
    #   Export / Download Report
    # ─────────────────────────────────────────────────────────
    @app.get("/export")
    async def export_csv(include_transcript: bool = False):
        """Generate and download the call results CSV report."""
        path = await generate_output_csv(
            _db,
            _settings.output_csv_dir,
            run_id="",
            include_transcript=include_transcript,
        )
        return FileResponse(
            path=str(path),
            media_type="text/csv",
            filename=path.name,
        )

    # ─────────────────────────────────────────────────────────
    #   WebSocket Media Stream (Twilio ↔ OpenAI Realtime)
    # ─────────────────────────────────────────────────────────
    @app.websocket("/ws/media-stream")
    async def media_stream_ws(websocket: WebSocket):
        await handle_media_stream(websocket, _settings, db=_db)

    # ─────────────────────────────────────────────────────────
    #   Twilio Webhooks
    # ─────────────────────────────────────────────────────────
    @app.post("/webhook/twilio/voice")
    async def twilio_voice_webhook(request: Request):
        """Twilio voice webhook — returns TwiML when call connects."""
        twiml = await handle_twilio_voice(request, _db, _settings)
        return Response(content=twiml, media_type="application/xml")

    @app.post("/webhook/twilio/status")
    async def twilio_status_webhook(request: Request):
        """Twilio status callback — processes call completion."""
        return await handle_twilio_status(request, _db, _settings)

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
