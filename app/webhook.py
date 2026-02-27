"""
FastAPI webhook receiver for VAPI call events.
Processes end-of-call reports and updates the database.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Optional

import structlog
from fastapi import FastAPI, Header, HTTPException, Request

from app.config import Settings
from app.database import Database
from app.models import Disposition

log = structlog.get_logger(__name__)

# Map VAPI ended reasons to our dispositions
_ENDED_REASON_MAP: dict[str, Disposition] = {
    "customer-did-not-answer": Disposition.NO_ANSWER,
    "customer-busy": Disposition.BUSY,
    "voicemail": Disposition.VOICEMAIL,
    "machine-detected": Disposition.VOICEMAIL,
    "customer-did-not-pick-up": Disposition.NO_ANSWER,
    "silence-timed-out": Disposition.NO_ANSWER,
    "phone-call-provider-closed-websocket": Disposition.FAILED,
    "error": Disposition.FAILED,
    "pipeline-error": Disposition.FAILED,
}


def _parse_disposition_from_analysis(analysis: Optional[dict]) -> Optional[Disposition]:
    """Extract disposition from VAPI structured analysis data."""
    if not analysis:
        return None

    structured = analysis.get("structuredData") or analysis.get("structured_data") or {}
    disp_str = structured.get("disposition", "")

    try:
        return Disposition(disp_str)
    except ValueError:
        return None


def _extract_analysis_fields(analysis: Optional[dict]) -> dict:
    """Extract summary, location, availability from VAPI analysis."""
    if not analysis:
        return {}

    structured = analysis.get("structuredData") or analysis.get("structured_data") or {}
    summary_data = analysis.get("summary", "")

    return {
        "summary": structured.get("summary", "") or summary_data,
        "location": structured.get("location", ""),
        "availability": structured.get("availability", ""),
    }


def create_webhook_app(settings: Settings, db: Database) -> FastAPI:
    """Create and return the FastAPI app with webhook routes."""

    app = FastAPI(
        title="AI Recruitment Caller — Webhook Receiver",
        version="0.1.0",
    )

    # ── Health check ────────────────────────────────────────────
    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # ── VAPI webhook endpoint ───────────────────────────────────
    @app.post("/webhook/vapi")
    async def vapi_webhook(
        request: Request,
        x_vapi_signature: Optional[str] = Header(None, alias="x-vapi-signature"),
    ):
        body = await request.body()

        # ── Signature verification (optional but recommended) ───
        if settings.webhook_secret and settings.webhook_secret != "change_me":
            if x_vapi_signature:
                expected = hmac.new(
                    settings.webhook_secret.encode(),
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

        # ── Handle different message types ──────────────────────
        if message_type == "end-of-call-report":
            await _handle_end_of_call(payload, db)
        elif message_type == "status-update":
            await _handle_status_update(payload, db)
        elif message_type == "hang":
            await _handle_hang(payload, db)
        elif message_type == "function-call":
            # Not used in MVP but placeholder for future
            pass

        # VAPI expects a 200 response
        return {"ok": True}

    return app


async def _handle_end_of_call(payload: dict, db: Database) -> None:
    """Process end-of-call report from VAPI."""
    message = payload.get("message", {})
    call = message.get("call", {})
    call_id = call.get("id", "")

    if not call_id:
        log.warning("end_of_call_missing_call_id", payload=payload)
        return

    # Look up the record by VAPI call ID
    record = await db.get_record_by_call_id(call_id)
    if not record:
        # Try metadata fallback
        metadata = call.get("metadata", {})
        record_id = metadata.get("unique_record_id", "")
        if record_id:
            record = await db.get_record_by_id(record_id)
        if not record:
            log.warning("end_of_call_record_not_found", call_id=call_id)
            return

    ended_reason = call.get("endedReason", "")
    transcript = message.get("transcript", "")
    recording_url = message.get("recordingUrl", "") or call.get("recordingUrl", "")
    analysis = message.get("analysis") or call.get("analysis")

    # ── Determine disposition ───────────────────────────────────
    # Priority: analysis > ended_reason mapping > FAILED
    disposition = _parse_disposition_from_analysis(analysis)

    if not disposition:
        disposition = _ENDED_REASON_MAP.get(ended_reason, Disposition.FAILED)

    # ── Extract analysis fields ─────────────────────────────────
    analysis_fields = _extract_analysis_fields(analysis)

    short_summary = analysis_fields.get("summary", "")
    if not short_summary and ended_reason:
        short_summary = f"Call ended: {ended_reason}"

    # ── Update database ─────────────────────────────────────────
    await db.update_call_result(
        vapi_call_id=call_id,
        status=disposition,
        short_summary=short_summary,
        raw_call_outcome=ended_reason,
        transcript=transcript,
        recording_url=recording_url,
        extracted_location=analysis_fields.get("location", ""),
        extracted_availability=analysis_fields.get("availability", ""),
    )

    log.info(
        "call_result_saved",
        call_id=call_id,
        record_id=record.unique_record_id,
        disposition=disposition.value,
        summary=short_summary[:100],
    )


async def _handle_status_update(payload: dict, db: Database) -> None:
    """Handle real-time status updates (ringing, in-progress, etc.)."""
    message = payload.get("message", {})
    call = message.get("call", {})
    call_id = call.get("id", "")
    status = message.get("status", "")

    log.info("call_status_update", call_id=call_id, status=status)


async def _handle_hang(payload: dict, db: Database) -> None:
    """Handle hang/disconnect events."""
    message = payload.get("message", {})
    call = message.get("call", {})
    call_id = call.get("id", "")

    log.info("call_hang_detected", call_id=call_id)
