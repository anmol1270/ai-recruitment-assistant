"""
SaaS server — multi-tenant AI Recruitment Caller platform.

Routes:
  Auth:     /auth/login, /auth/callback, /auth/logout, /auth/me
  Campaign: /api/campaigns (CRUD), /api/campaigns/:id/upload, /api/campaigns/:id/start
  Status:   /api/campaigns/:id/status, /api/campaigns/:id/candidates
  Export:   /api/campaigns/:id/export
  Billing:  /api/billing/checkout, /api/billing/portal, /webhook/stripe
  Webhook:  /webhook/vapi
  UI:       / (dashboard)
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import httpx
import io
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.auth import AuthManager
from app.billing import BillingManager, PLANS
from app.config import get_settings
from app.csv_pipeline import ingest_csv
from app.phone_utils import normalise_phone
from app.saas_db import SaaSDatabase
from app.vapi_client import VAPIClient
from app.webhook import (
    _parse_disposition_from_analysis,
    _extract_analysis_fields,
    _infer_disposition_from_summary,
    _cross_check_disposition,
    _ENDED_REASON_MAP,
)

log = structlog.get_logger(__name__)

# Module-level references — populated during lifespan
_db: Optional[SaaSDatabase] = None
_settings = None
_auth: Optional[AuthManager] = None
_billing: Optional[BillingManager] = None
_vapi: Optional[VAPIClient] = None
_active_tasks: dict[int, asyncio.Task] = {}  # campaign_id -> task


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db, _settings, _auth, _billing, _vapi
    _settings = get_settings()
    _settings.ensure_dirs()

    # PostgreSQL
    if _settings.database_url:
        _db = SaaSDatabase(_settings.database_url)
        await _db.connect()
    else:
        log.warning("no_database_url", msg="Set DATABASE_URL for PostgreSQL")

    # Auth
    _auth = AuthManager(
        google_client_id=_settings.google_client_id,
        google_client_secret=_settings.google_client_secret,
        jwt_secret=_settings.jwt_secret,
        base_url=_settings.webhook_base_url,
    )

    # Billing
    if _settings.stripe_secret_key:
        _billing = BillingManager(
            stripe_secret_key=_settings.stripe_secret_key,
            stripe_webhook_secret=_settings.stripe_webhook_secret,
            stripe_pro_price_id=_settings.stripe_pro_price_id,
            base_url=_settings.webhook_base_url,
        )

    # VAPI
    _vapi = VAPIClient(_settings)

    log.info("saas_server_started")
    yield
    if _vapi:
        await _vapi.close()
    if _db:
        await _db.close()
    log.info("saas_server_stopped")


def create_saas_app() -> FastAPI:
    app = FastAPI(title="AI Recruitment Caller SaaS", version="1.0.0", lifespan=lifespan)

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ═══════════════════════════════════════════════════════════
    #  Health check
    # ═══════════════════════════════════════════════════════════
    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "1.0.0"}

    # ═══════════════════════════════════════════════════════════
    #  Auth routes
    # ═══════════════════════════════════════════════════════════
    @app.get("/auth/login")
    async def auth_login():
        url = _auth.get_login_url()
        return RedirectResponse(url=url)

    @app.get("/auth/callback")
    async def auth_callback(code: str = Query(...)):
        user_info = await _auth.exchange_code(code)
        google_id = user_info.get("id", "")
        email = user_info.get("email", "")
        name = user_info.get("name", "")
        avatar = user_info.get("picture", "")

        # Create or update user
        user = await _db.create_user(
            google_id=google_id, email=email, name=name, avatar_url=avatar
        )

        # Create JWT session
        token = _auth.create_session_token(user["id"], email)
        response = RedirectResponse(url="/", status_code=302)
        _auth.set_session_cookie(response, token)
        log.info("user_logged_in", user_id=user["id"], email=email)
        return response

    @app.get("/auth/logout")
    async def auth_logout():
        response = RedirectResponse(url="/", status_code=302)
        _auth.clear_session_cookie(response)
        return response

    @app.get("/auth/me")
    async def auth_me(request: Request):
        user_id = _auth.get_current_user_id(request)
        if not user_id:
            return {"authenticated": False}
        user = await _db.get_user_by_id(user_id)
        if not user:
            return {"authenticated": False}
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        usage = await _db.get_usage(user_id, month)
        return {
            "authenticated": True,
            "user": {
                "id": user["id"],
                "email": user["email"],
                "name": user["name"],
                "avatar_url": user["avatar_url"],
                "plan": user["plan"],
                "calls_this_month": usage["calls_made"],
                "monthly_limit": usage["calls_limit"],
            },
        }

    # ═══════════════════════════════════════════════════════════
    #  Campaign routes
    # ═══════════════════════════════════════════════════════════
    @app.get("/api/campaigns")
    async def list_campaigns(request: Request):
        user_id = _auth.require_auth(request)
        campaigns = await _db.get_campaigns(user_id)
        return {"campaigns": campaigns}

    @app.post("/api/campaigns")
    async def create_campaign(request: Request):
        user_id = _auth.require_auth(request)
        body = await request.json()
        name = body.get("name", "").strip()
        job_role = body.get("job_role", "").strip()
        description = body.get("description", "")
        custom_prompt = body.get("custom_prompt", "")

        if not name or not job_role:
            raise HTTPException(400, "Name and job_role are required")

        campaign = await _db.create_campaign(
            user_id=user_id,
            name=name,
            job_role=job_role,
            description=description,
            custom_prompt=custom_prompt,
        )
        return {"campaign": campaign}

    @app.get("/api/campaigns/{campaign_id}")
    async def get_campaign(campaign_id: int, request: Request):
        user_id = _auth.require_auth(request)
        campaign = await _db.get_campaign(campaign_id, user_id)
        if not campaign:
            raise HTTPException(404, "Campaign not found")
        stats = await _db.get_campaign_stats(campaign_id, user_id)
        return {"campaign": campaign, "stats": stats}

    @app.delete("/api/campaigns/{campaign_id}")
    async def delete_campaign(campaign_id: int, request: Request):
        user_id = _auth.require_auth(request)
        ok = await _db.delete_campaign(campaign_id, user_id)
        if not ok:
            raise HTTPException(404, "Campaign not found")
        return {"ok": True}

    # ── Upload candidates to campaign ───────────────────────────
    @app.post("/api/campaigns/{campaign_id}/upload")
    async def upload_candidates(
        campaign_id: int,
        request: Request,
        file: UploadFile = File(...),
    ):
        user_id = _auth.require_auth(request)
        campaign = await _db.get_campaign(campaign_id, user_id)
        if not campaign:
            raise HTTPException(404, "Campaign not found")

        if not file.filename or not file.filename.endswith(".csv"):
            raise HTTPException(400, "File must be a .csv")

        # Save temp
        content = await file.read()
        tmp = _settings.input_csv_dir / f"upload_{uuid.uuid4().hex[:8]}_{file.filename}"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(content)

        # Parse
        try:
            suppression = (
                _settings.suppression_list_path
                if _settings.suppression_list_path.exists()
                else None
            )
            valid, rejected = ingest_csv(tmp, suppression)
        except ValueError as e:
            raise HTTPException(400, str(e))

        # Insert
        candidates = []
        for v in valid:
            candidates.append({
                "unique_record_id": v.unique_record_id,
                "first_name": v.first_name,
                "last_name": v.last_name,
                "phone_e164": v.phone_e164,
                "email": v.email or "",
            })

        count = await _db.add_candidates(campaign_id, user_id, candidates)

        rejection_details = []
        for r in rejected[:20]:
            rejection_details.append({
                "row": r.get("_row", "?"),
                "reason": r.get("_reason", "unknown"),
            })

        return {
            "status": "ok",
            "valid_records": count,
            "rejected_records": len(rejected),
            "rejected_details": rejection_details,
        }

    # ── Start calls for campaign ────────────────────────────────
    @app.post("/api/campaigns/{campaign_id}/start")
    async def start_campaign_calls(campaign_id: int, request: Request):
        user_id = _auth.require_auth(request)
        campaign = await _db.get_campaign(campaign_id, user_id)
        if not campaign:
            raise HTTPException(404, "Campaign not found")

        # Check usage limits
        can_call = await _db.can_place_call(user_id)
        if not can_call:
            raise HTTPException(
                402,
                "Monthly call limit reached. Upgrade to Pro for more calls.",
            )

        # Check if already running
        if campaign_id in _active_tasks and not _active_tasks[campaign_id].done():
            return {"status": "already_running"}

        # Get or create VAPI assistant for this campaign
        assistant_id = campaign.get("vapi_assistant_id", "")
        if not assistant_id:
            try:
                assistant_id = await _vapi.create_assistant()
                await _db.update_campaign_assistant(campaign_id, assistant_id)
            except Exception as e:
                log.error("vapi_assistant_creation_failed", error=str(e))
                raise HTTPException(503, f"Failed to create VAPI assistant: {e}")

        # Start calling in background
        async def _run():
            pending = await _db.get_pending_candidates(campaign_id)
            month = datetime.now(timezone.utc).strftime("%Y-%m")
            placed = 0
            errors = 0

            await _db.update_campaign_status(campaign_id, user_id, "active")

            for candidate in pending:
                # Check per-call limit
                if not await _db.can_place_call(user_id):
                    log.info("monthly_limit_reached", user_id=user_id)
                    break

                try:
                    result = await _vapi.place_call(
                        phone_e164=candidate["phone_e164"],
                        assistant_id=assistant_id,
                        candidate_name=candidate.get("first_name") or candidate["unique_record_id"],
                        record_id=candidate["unique_record_id"],
                        job_role=campaign["job_role"],
                    )
                    vapi_call_id = result.get("id", "")
                    await _db.mark_call_started(candidate["id"], vapi_call_id)
                    await _db.increment_usage(user_id, month)
                    await _db.log_call_event(
                        user_id=user_id,
                        campaign_id=campaign_id,
                        candidate_id=candidate["id"],
                        vapi_call_id=vapi_call_id,
                        action="call_placed",
                        status="in_progress",
                    )
                    placed += 1
                    await asyncio.sleep(2)  # Pace calls
                except Exception as e:
                    log.error("call_error", candidate_id=candidate["id"], error=str(e))
                    errors += 1

            await _db.update_campaign_status(campaign_id, user_id, "completed")
            log.info("campaign_calls_done", campaign_id=campaign_id, placed=placed, errors=errors)

        _active_tasks[campaign_id] = asyncio.create_task(_run())

        return {
            "status": "started",
            "message": f"Calling started for campaign '{campaign['name']}'",
        }

    # ── Campaign status ─────────────────────────────────────────
    @app.get("/api/campaigns/{campaign_id}/status")
    async def campaign_status(campaign_id: int, request: Request):
        user_id = _auth.require_auth(request)
        campaign = await _db.get_campaign(campaign_id, user_id)
        if not campaign:
            raise HTTPException(404, "Campaign not found")

        stats = await _db.get_campaign_stats(campaign_id, user_id)
        active = campaign_id in _active_tasks and not _active_tasks[campaign_id].done()

        return {
            "campaign": campaign,
            "stats": stats,
            "calling_active": active,
        }

    # ── Campaign candidates ─────────────────────────────────────
    @app.get("/api/campaigns/{campaign_id}/candidates")
    async def campaign_candidates(campaign_id: int, request: Request):
        user_id = _auth.require_auth(request)
        candidates = await _db.get_candidates(campaign_id, user_id)
        return {"candidates": candidates}

    # ── Export campaign report ──────────────────────────────────
    @app.get("/api/campaigns/{campaign_id}/export")
    async def export_campaign(campaign_id: int, request: Request):
        user_id = _auth.require_auth(request)
        campaign = await _db.get_campaign(campaign_id, user_id)
        if not campaign:
            raise HTTPException(404, "Campaign not found")

        candidates = await _db.get_candidates(campaign_id, user_id)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "unique_record_id", "first_name", "last_name", "phone",
            "email", "status", "summary", "location", "availability",
            "attempt_count", "last_called_at", "recording_url",
        ])
        for c in candidates:
            writer.writerow([
                c.get("unique_record_id", ""),
                c.get("first_name", ""),
                c.get("last_name", ""),
                c.get("phone_e164", ""),
                c.get("email", ""),
                c.get("status", ""),
                c.get("short_summary", ""),
                c.get("extracted_location", ""),
                c.get("extracted_availability", ""),
                c.get("attempt_count", 0),
                str(c.get("last_called_at", "") or ""),
                c.get("recording_url", ""),
            ])

        output.seek(0)
        filename = f"{campaign['name'].replace(' ', '_')}_report.csv"
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ── User dashboard stats ────────────────────────────────────
    @app.get("/api/stats")
    async def user_stats(request: Request):
        user_id = _auth.require_auth(request)
        stats = await _db.get_user_stats(user_id)
        return stats

    # ═══════════════════════════════════════════════════════════
    #  Billing routes
    # ═══════════════════════════════════════════════════════════
    @app.post("/api/billing/checkout")
    async def billing_checkout(request: Request):
        user_id = _auth.require_auth(request)
        if not _billing:
            raise HTTPException(503, "Billing not configured")

        user = await _db.get_user_by_id(user_id)
        customer_id = user.get("stripe_customer_id", "")
        if not customer_id:
            customer_id = await _billing.get_or_create_customer(
                user_id, user["email"], user.get("name", "")
            )
            await _db.update_user_stripe(user_id, customer_id)

        url = await _billing.create_checkout_session(customer_id, user_id)
        return {"url": url}

    @app.post("/api/billing/portal")
    async def billing_portal(request: Request):
        user_id = _auth.require_auth(request)
        if not _billing:
            raise HTTPException(503, "Billing not configured")

        user = await _db.get_user_by_id(user_id)
        customer_id = user.get("stripe_customer_id", "")
        if not customer_id:
            raise HTTPException(400, "No billing account found")

        url = await _billing.create_portal_session(customer_id)
        return {"url": url}

    @app.post("/webhook/stripe")
    async def stripe_webhook(
        request: Request,
        stripe_signature: str = Header(None, alias="stripe-signature"),
    ):
        if not _billing:
            return {"ok": True}

        body = await request.body()
        event = _billing.verify_webhook(body, stripe_signature or "")
        if not event:
            raise HTTPException(400, "Invalid webhook signature")

        event_type = event["type"]
        log.info("stripe_webhook", event_type=event_type)

        if event_type == "checkout.session.completed":
            session = event["data"]["object"]
            user_id = int(session.get("metadata", {}).get("user_id", 0))
            subscription_id = session.get("subscription", "")
            customer_id = session.get("customer", "")

            if user_id:
                await _db.update_user_plan(
                    user_id=user_id,
                    plan="pro",
                    monthly_call_limit=PLANS["pro"]["calls"],
                    stripe_customer_id=customer_id,
                    stripe_subscription_id=subscription_id,
                )
                log.info("user_upgraded_to_pro", user_id=user_id)

        elif event_type in (
            "customer.subscription.deleted",
            "customer.subscription.updated",
        ):
            sub = event["data"]["object"]
            customer_id = sub.get("customer", "")
            status = sub.get("status", "")

            if status in ("canceled", "unpaid", "past_due"):
                # Find user by stripe customer ID and downgrade
                async with _db._pool.acquire() as conn:
                    user = await conn.fetchrow(
                        "SELECT id FROM users WHERE stripe_customer_id = $1",
                        customer_id,
                    )
                    if user:
                        await _db.update_user_plan(
                            user_id=user["id"],
                            plan="free",
                            monthly_call_limit=PLANS["free"]["calls"],
                            stripe_customer_id=customer_id,
                            stripe_subscription_id="",
                        )
                        log.info("user_downgraded", user_id=user["id"])

        return {"ok": True}

    # ═══════════════════════════════════════════════════════════
    #  VAPI Webhook (call results)
    # ═══════════════════════════════════════════════════════════
    @app.post("/webhook/vapi")
    async def vapi_webhook(
        request: Request,
        x_vapi_signature: Optional[str] = Header(None, alias="x-vapi-signature"),
    ):
        body = await request.body()

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(400, "Invalid JSON")

        message_type = payload.get("message", {}).get("type", "")
        log.info("vapi_webhook", message_type=message_type)

        if message_type == "end-of-call-report":
            await _handle_vapi_end_of_call(payload)

        return {"ok": True}

    async def _handle_vapi_end_of_call(payload: dict):
        """Process VAPI end-of-call-report and update candidate."""
        message = payload.get("message", {})
        call = message.get("call", {})
        call_id = call.get("id", "")
        if not call_id:
            return

        # Find the candidate
        candidate = await _db.get_candidate_by_call_id(call_id)
        if not candidate:
            metadata = call.get("metadata", {})
            record_id = metadata.get("unique_record_id", "")
            if record_id:
                candidate = await _db.get_candidate_by_record_id(record_id)
            if not candidate:
                log.warning("vapi_candidate_not_found", call_id=call_id)
                return

        ended_reason = call.get("endedReason", "")
        transcript = message.get("transcript", "")
        recording_url = message.get("recordingUrl", "") or call.get("recordingUrl", "")
        analysis = message.get("analysis") or call.get("analysis")

        # Extract analysis
        analysis_fields = _extract_analysis_fields(analysis)

        # Determine disposition
        disposition = _parse_disposition_from_analysis(analysis)
        if not disposition:
            mapped = _ENDED_REASON_MAP.get(ended_reason)
            if mapped is not None:
                disposition = mapped
            else:
                disposition = _infer_disposition_from_summary(
                    analysis_fields.get("summary", "")
                )

        short_summary = analysis_fields.get("summary", "")
        if not short_summary and ended_reason:
            short_summary = f"Call ended: {ended_reason}"

        # Cross-check
        disposition = _cross_check_disposition(disposition, short_summary)

        # Update DB
        await _db.update_call_result(
            vapi_call_id=call_id,
            status=disposition.value,
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
            disposition=disposition.value,
        )

    # ═══════════════════════════════════════════════════════════
    #  Quick test call (debug)
    # ═══════════════════════════════════════════════════════════
    @app.post("/api/test-call")
    async def test_call(request: Request):
        user_id = _auth.require_auth(request)
        body = await request.json()
        phone = body.get("phone", "")
        name = body.get("name", "Test")

        if not phone:
            raise HTTPException(400, "phone is required")

        # Normalize
        e164, valid = normalise_phone(phone)
        if not valid:
            raise HTTPException(400, f"Invalid phone number: {phone}")

        try:
            assistant_id = await _vapi.get_or_create_assistant()
            result = await _vapi.place_call(
                phone_e164=e164,
                assistant_id=assistant_id,
                candidate_name=name,
                record_id="test-debug",
                job_role="Test Call",
            )
            return {"status": "ok", "vapi_response": result}
        except httpx.HTTPStatusError as e:
            return {
                "status": "error",
                "error": str(e),
                "response_body": e.response.text,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    # ═══════════════════════════════════════════════════════════
    #  Dashboard UI
    # ═══════════════════════════════════════════════════════════
    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        index_path = static_dir / "index.html"
        if index_path.exists():
            return HTMLResponse(content=index_path.read_text(), status_code=200)
        return HTMLResponse("<h1>AI Recruitment Caller</h1>")

    return app


# Default app instance
app = create_saas_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.saas_server:app", host="0.0.0.0", port=8000, reload=False)
