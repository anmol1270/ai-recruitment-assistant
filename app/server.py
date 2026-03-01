"""
Combined server entry point — runs the webhook receiver.

Usage:
    python -m app.server
    # or
    uvicorn app.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import structlog
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from app.config import get_settings
from app.csv_pipeline import ingest_csv
from app.database import Database
from app.models import CallRecord, Disposition
from app.output import generate_output_csv, generate_rejected_csv, generate_run_summary
from app.scheduler import CallScheduler
from app.vapi_client import VAPIClient
from app.webhook import (
    _handle_end_of_call,
    _handle_hang,
    _handle_status_update,
)

log = structlog.get_logger(__name__)

# Module-level references populated during lifespan
_db: Optional[Database] = None
_settings = None
_vapi: Optional[VAPIClient] = None
_assistant_id: str = ""
_active_call_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start up: connect DB, init VAPI. Shut down: close both."""
    global _db, _settings, _vapi, _assistant_id
    _settings = get_settings()
    _settings.ensure_dirs()
    _db = Database(_settings.database_path)
    await _db.connect()
    _vapi = VAPIClient(_settings)
    try:
        _assistant_id = await _vapi.get_or_create_assistant()
    except Exception as e:
        log.warning("vapi_assistant_init_failed", error=str(e))
        _assistant_id = _settings.vapi_assistant_id or ""
    log.info("server_started", db=str(_settings.database_path), assistant_id=_assistant_id)
    yield
    if _vapi:
        await _vapi.close()
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

    @app.get("/")
    async def root():
        return {"message": "AI Recruitment Caller API", "version": "0.2.0"}

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

        if not _assistant_id:
            raise HTTPException(
                status_code=503,
                detail="VAPI assistant not configured. Set VAPI_API_KEY and VAPI_PHONE_NUMBER_ID.",
            )

        if _active_call_task and not _active_call_task.done():
            return {"status": "already_running", "message": "A calling batch is already in progress."}

        run_id = uuid.uuid4().hex[:12]

        async def _run_calls():
            scheduler = CallScheduler(_settings, _db, _vapi, run_id)
            try:
                stats = await scheduler.run_batch(_assistant_id)
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
    #   VAPI Webhook
    # ─────────────────────────────────────────────────────────
    @app.post("/webhook/vapi")
    async def vapi_webhook(
        request: Request,
        x_vapi_signature: Optional[str] = Header(None, alias="x-vapi-signature"),
    ):
        body = await request.body()

        # Signature verification
        if _settings and _settings.webhook_secret and _settings.webhook_secret != "change_me":
            if x_vapi_signature:
                expected = hmac.new(
                    _settings.webhook_secret.encode(),
                    body,
                    hashlib.sha256,
                ).hexdigest()
                if not hmac.compare_digest(expected, x_vapi_signature):
                    log.warning("webhook_signature_mismatch")
                    raise HTTPException(status_code=401, detail="Invalid signature")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        message_type = payload.get("message", {}).get("type", "")

        log.info(
            "webhook_received",
            message_type=message_type,
            call_id=payload.get("message", {}).get("call", {}).get("id", ""),
        )

        if message_type == "end-of-call-report":
            await _handle_end_of_call(payload, _db)
        elif message_type == "status-update":
            await _handle_status_update(payload, _db)
        elif message_type == "hang":
            await _handle_hang(payload, _db)

        return {"ok": True}

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
