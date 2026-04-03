"""
SaaS server — multi-tenant AI Recruitment Caller platform.

Routes:
  Auth:     /auth/login, /auth/callback, /auth/logout, /auth/me
  Campaign: /api/campaigns (CRUD), /api/campaigns/:id/process (upload+rank+call)
  Status:   /api/campaigns/:id/status, /api/campaigns/:id/candidates
  Export:   /api/campaigns/:id/export
  Billing:  /api/billing/checkout, /api/billing/portal, /webhook/stripe
  Webhook:  /webhook/twilio/voice, /webhook/twilio/status
  WS:       /ws/media-stream (Twilio ↔ OpenAI Realtime)
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
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.auth import AuthManager
from app.billing import BillingManager, PLANS
from app.config import get_settings
from app.csv_pipeline import ingest_csv
from app.phone_utils import normalise_phone
from app.resume_parser import parse_resumes_from_zip
from app.ats_ranker import ATSRanker
from app.saas_db import SaaSDatabase
from app.twilio_service import TwilioService
from app.media_stream import handle_media_stream
from app.webhook import (
    _parse_disposition_from_text,
    _cross_check_disposition,
    _TWILIO_STATUS_MAP,
    analyse_transcript_with_openai,
    handle_twilio_voice,
    handle_twilio_status,
)

log = structlog.get_logger(__name__)

# Admin email list (parsed from comma-separated env var at startup)
_admin_emails: set[str] = set()

# Module-level references — populated during lifespan
_db: Optional[SaaSDatabase] = None
_settings = None
_auth: Optional[AuthManager] = None
_billing: Optional[BillingManager] = None
_twilio: Optional[TwilioService] = None
_ranker: Optional[ATSRanker] = None
_active_tasks: dict[int, asyncio.Task] = {}  # campaign_id -> task


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db, _settings, _auth, _billing, _twilio, _admin_emails
    _settings = get_settings()
    _settings.ensure_dirs()

    # Parse admin emails
    if _settings.admin_emails:
        _admin_emails = {e.strip().lower() for e in _settings.admin_emails.split(",") if e.strip()}
        log.info("admin_emails_loaded", count=len(_admin_emails))

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
            stripe_starter_price_id=_settings.stripe_starter_price_id,
            stripe_pro_price_id=_settings.stripe_pro_price_id,
            stripe_enterprise_price_id=_settings.stripe_enterprise_price_id,
            base_url=_settings.webhook_base_url,
        )

    # Twilio
    if _settings.twilio_account_sid:
        _twilio = TwilioService(_settings)
        log.info("twilio_service_ready")
    else:
        log.warning("no_twilio_credentials", msg="Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN")

    # ATS Ranker
    global _ranker
    if _settings.openai_api_key:
        _ranker = ATSRanker(_settings.openai_api_key)
    else:
        log.warning("no_openai_api_key", msg="Set OPENAI_API_KEY for resume ranking")

    log.info("saas_server_started")
    yield
    if _twilio:
        await _twilio.close()
    if _ranker:
        await _ranker.close()
    if _db:
        await _db.close()
    log.info("saas_server_stopped")


def create_saas_app() -> FastAPI:
    app = FastAPI(title="AI Recruitment Caller SaaS", version="1.0.0", lifespan=lifespan)

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ── DB-availability middleware ─────────────────────────────
    @app.middleware("http")
    async def db_guard(request: Request, call_next):
        path = request.url.path
        # Allow health, static, and dashboard root through even without DB
        if path in ("/health", "/", "/docs", "/openapi.json") or path.startswith("/static"):
            return await call_next(request)
        if _db is None:
            return JSONResponse(
                {"detail": "Database not connected. Set DATABASE_URL on Railway."},
                status_code=503,
            )
        return await call_next(request)

    # ── helpers ──────────────────────────────────────────────
    def _require_db():
        """Raise 503 if the database is not connected."""
        if _db is None:
            raise HTTPException(
                503,
                "Database not connected. Set DATABASE_URL environment variable.",
            )
        return _db

    async def _maybe_assign_admin(user: dict) -> None:
        """If user email is in admin list, upgrade to admin plan."""
        email = user.get("email", "").lower()
        if email in _admin_emails and user.get("plan") != "admin":
            await _db.update_user_plan(
                user_id=user["id"],
                plan="admin",
                monthly_call_limit=PLANS["admin"]["calls"],
            )
            log.info("admin_plan_assigned", user_id=user["id"], email=email)

    # ═══════════════════════════════════════════════════════════
    #  Health check
    # ═══════════════════════════════════════════════════════════
    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "1.0.0", "db": _db is not None}

    # ═══════════════════════════════════════════════════════════
    #  Auth routes
    # ═══════════════════════════════════════════════════════════
    @app.get("/auth/login")
    async def auth_login():
        if _settings.google_client_id:
            url = _auth.get_login_url()
            return RedirectResponse(url=url)
        # No Google OAuth configured — redirect to home (use /auth/quick-login)
        return RedirectResponse(url="/", status_code=302)

    @app.post("/auth/quick-login")
    async def quick_login(request: Request):
        """Email-only login — no OAuth required. For dev/demo use."""
        body = await request.json()
        email = body.get("email", "").strip().lower()
        name = body.get("name", "").strip() or email.split("@")[0]

        if not email or "@" not in email:
            raise HTTPException(400, "Valid email required")

        # Create or fetch user (use email hash as pseudo google_id)
        db = _require_db()
        user = await db.get_user_by_email(email)
        if not user:
            google_id = f"email_{hashlib.sha256(email.encode()).hexdigest()[:16]}"
            user = await db.create_user(
                google_id=google_id, email=email, name=name, avatar_url=""
            )

        token = _auth.create_session_token(user["id"], email)
        response = JSONResponse({"ok": True, "email": email})
        _auth.set_session_cookie(response, token)
        await _maybe_assign_admin(user)
        log.info("quick_login", user_id=user["id"], email=email)
        return response

    @app.get("/auth/callback")
    async def auth_callback(code: str = Query(...)):
        user_info = await _auth.exchange_code(code)
        google_id = user_info.get("id", "")
        email = user_info.get("email", "")
        name = user_info.get("name", "")
        avatar = user_info.get("picture", "")

        # Create or update user
        db = _require_db()
        user = await db.create_user(
            google_id=google_id, email=email, name=name, avatar_url=avatar
        )

        # Create JWT session
        token = _auth.create_session_token(user["id"], email)
        response = RedirectResponse(url="/", status_code=302)
        _auth.set_session_cookie(response, token)
        await _maybe_assign_admin(user)
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

        # Enforce candidate limit for free trial
        user = await _db.get_user_by_id(user_id)
        user_plan = user.get("plan", "free")
        max_candidates = PLANS.get(user_plan, PLANS["free"]).get("max_candidates", 0)
        if max_candidates > 0 and len(candidates) > max_candidates:
            raise HTTPException(
                402,
                f"Your {PLANS[user_plan]['name']} plan allows up to {max_candidates} candidates per upload. Upgrade for unlimited.",
            )

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

    # ── Upload resumes (ZIP) and AI-rank them ───────────────────
    @app.post("/api/campaigns/{campaign_id}/upload-resumes")
    async def upload_resumes(
        campaign_id: int,
        request: Request,
        file: UploadFile = File(...),
    ):
        """Upload a ZIP of resumes → parse → AI rank → return results."""
        user_id = _auth.require_auth(request)
        campaign = await _db.get_campaign(campaign_id, user_id)
        if not campaign:
            raise HTTPException(404, "Campaign not found")

        if not _ranker:
            raise HTTPException(503, "Resume ranking not configured. Set OPENAI_API_KEY.")

        if not file.filename or not file.filename.lower().endswith(".zip"):
            raise HTTPException(400, "File must be a .zip archive containing resumes (PDF/DOCX)")

        zip_data = await file.read()
        if len(zip_data) > 50 * 1024 * 1024:  # 50 MB limit
            raise HTTPException(400, "ZIP file too large (max 50 MB)")

        # 1. Parse resumes from ZIP
        try:
            parsed = parse_resumes_from_zip(zip_data)
        except ValueError as e:
            raise HTTPException(400, str(e))

        if not parsed:
            raise HTTPException(400, "No valid resumes found in ZIP (supported: PDF, DOCX, TXT)")

        valid_resumes = [r for r in parsed if r.get("text") and not r.get("error")]
        if not valid_resumes:
            raise HTTPException(400, f"Could not extract text from any of the {len(parsed)} files")

        # 2. Build job description from campaign
        job_desc = f"Job Role: {campaign['job_role']}\n"
        if campaign.get("description"):
            job_desc += f"\n{campaign['description']}"
        if campaign.get("custom_prompt"):
            job_desc += f"\n\nAdditional Requirements:\n{campaign['custom_prompt']}"

        # 3. AI rank all resumes
        top_percent = _settings.ats_top_percent
        ranking_result = await _ranker.rank_resumes(
            resumes=valid_resumes,
            job_description=job_desc,
            top_percent=top_percent,
        )

        # 4. Clear previous rankings for this campaign and store new ones
        await _db.clear_resume_rankings(campaign_id, user_id)

        selected_set = {r["filename"] for r in ranking_result["selected"]}

        for r in ranking_result["all_ranked"]:
            await _db.add_resume_ranking(
                campaign_id=campaign_id,
                user_id=user_id,
                filename=r.get("filename", ""),
                full_name=r.get("full_name", ""),
                email=r.get("email", ""),
                phone=r.get("phone", ""),
                current_title=r.get("current_title", ""),
                years_experience=int(r.get("years_experience", 0)),
                resume_text=r.get("resume_text", ""),
                skills_match=r.get("skills_match", 0),
                experience_relevance=r.get("experience_relevance", 0),
                education_fit=r.get("education_fit", 0),
                overall_suitability=r.get("overall_suitability", 0),
                total_score=r.get("total_score", 0),
                reasoning=r.get("reasoning", ""),
                selected=r["filename"] in selected_set,
            )

        parse_errors = [r for r in parsed if r.get("error")]

        return {
            "status": "ok",
            "stats": ranking_result["stats"],
            "rankings": [
                {
                    "filename": r.get("filename"),
                    "full_name": r.get("full_name"),
                    "email": r.get("email"),
                    "phone": r.get("phone"),
                    "current_title": r.get("current_title"),
                    "total_score": r.get("total_score"),
                    "skills_match": r.get("skills_match"),
                    "experience_relevance": r.get("experience_relevance"),
                    "education_fit": r.get("education_fit"),
                    "overall_suitability": r.get("overall_suitability"),
                    "reasoning": r.get("reasoning"),
                    "selected": r["filename"] in selected_set,
                }
                for r in ranking_result["all_ranked"]
            ],
            "parse_errors": [
                {"filename": e["filename"], "error": e["error"]}
                for e in parse_errors[:10]
            ],
        }

    @app.get("/api/campaigns/{campaign_id}/rankings")
    async def get_rankings(campaign_id: int, request: Request):
        """Get resume rankings for a campaign."""
        user_id = _auth.require_auth(request)
        campaign = await _db.get_campaign(campaign_id, user_id)
        if not campaign:
            raise HTTPException(404, "Campaign not found")

        rankings = await _db.get_resume_rankings(campaign_id, user_id)
        stats = await _db.get_ranking_stats(campaign_id, user_id)

        return {
            "rankings": [
                {
                    "id": r["id"],
                    "filename": r["filename"],
                    "full_name": r["full_name"],
                    "email": r["email"],
                    "phone": r["phone"],
                    "current_title": r["current_title"],
                    "years_experience": r["years_experience"],
                    "total_score": r["total_score"],
                    "skills_match": r["skills_match"],
                    "experience_relevance": r["experience_relevance"],
                    "education_fit": r["education_fit"],
                    "overall_suitability": r["overall_suitability"],
                    "reasoning": r["reasoning"],
                    "selected": r["selected"],
                    "promoted_to_candidate": r["promoted_to_candidate"],
                }
                for r in rankings
            ],
            "stats": stats,
        }

    @app.post("/api/campaigns/{campaign_id}/promote-rankings")
    async def promote_rankings(campaign_id: int, request: Request):
        """
        Promote selected ranked resumes to the calling pipeline.
        Creates candidates from the top-ranked resumes that have phone numbers.
        Optionally pass {"ranking_ids": [1,2,3]} to promote specific ones,
        or omit to promote all selected.
        """
        user_id = _auth.require_auth(request)
        campaign = await _db.get_campaign(campaign_id, user_id)
        if not campaign:
            raise HTTPException(404, "Campaign not found")

        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        ranking_ids = body.get("ranking_ids", None)

        # Get rankings to promote
        if ranking_ids:
            rankings = await _db.get_resume_rankings(campaign_id, user_id)
            to_promote = [r for r in rankings if r["id"] in ranking_ids]
        else:
            to_promote = await _db.get_resume_rankings(campaign_id, user_id, selected_only=True)

        # Filter: must have a phone number
        promoted = []
        skipped_no_phone = 0
        for r in to_promote:
            phone_raw = r.get("phone", "").strip()
            if not phone_raw:
                skipped_no_phone += 1
                continue

            # Try to normalise the phone
            e164, valid = normalise_phone(phone_raw)
            if not valid:
                skipped_no_phone += 1
                continue

            # Split full_name into first/last
            name_parts = (r.get("full_name", "") or "").strip().split(maxsplit=1)
            first_name = name_parts[0] if name_parts else ""
            last_name = name_parts[1] if len(name_parts) > 1 else ""

            promoted.append({
                "unique_record_id": f"resume_{r['id']}_{uuid.uuid4().hex[:6]}",
                "first_name": first_name,
                "last_name": last_name,
                "phone_e164": e164,
                "email": r.get("email", ""),
            })

        if not promoted:
            raise HTTPException(
                400,
                f"No candidates could be promoted. {skipped_no_phone} resumes had no valid phone number."
            )

        # Insert as candidates
        count = await _db.add_candidates(campaign_id, user_id, promoted)

        # Mark as promoted in rankings table
        promoted_ids = [r["id"] for r in to_promote if r.get("phone")]
        if promoted_ids:
            await _db.mark_rankings_promoted(campaign_id, user_id, promoted_ids)

        return {
            "status": "ok",
            "promoted": count,
            "skipped_no_phone": skipped_no_phone,
            "message": f"{count} candidates added to calling pipeline from resume rankings.",
        }

    # ── Start calls for campaign ────────────────────────────────

    async def _auto_start_calls(campaign_id: int, user_id: int, from_number: str, campaign: dict):
        """Background task: call all pending candidates in a campaign."""
        pending = await _db.get_pending_candidates(campaign_id)
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        placed = 0
        errors = 0

        await _db.update_campaign_status(campaign_id, user_id, "active")

        for candidate in pending:
            if not await _db.can_place_call(user_id):
                log.info("monthly_limit_reached", user_id=user_id)
                break

            try:
                result = await _twilio.place_call(
                    phone_e164=candidate["phone_e164"],
                    from_number=from_number,
                    candidate_name=candidate.get("first_name") or candidate["unique_record_id"],
                    record_id=candidate["unique_record_id"],
                    job_role=campaign["job_role"],
                    campaign_id=campaign_id,
                )
                call_sid = result.get("id", "")
                await _db.mark_call_started(candidate["id"], call_sid)
                await _db.increment_usage(user_id, month)
                await _db.log_call_event(
                    user_id=user_id,
                    campaign_id=campaign_id,
                    candidate_id=candidate["id"],
                    vapi_call_id=call_sid,
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

    @app.post("/api/campaigns/{campaign_id}/process")
    async def process_resumes_and_call(
        campaign_id: int,
        request: Request,
        file: UploadFile = File(...),
        job_description: str = Form(default=""),
    ):
        """
        All-in-one endpoint: upload ZIP of resumes → AI rank → promote top candidates → start calling.

        Form fields:
          - file: ZIP archive containing resumes (PDF/DOCX/TXT)
          - job_description: the full job description to rank against
        """
        user_id = _auth.require_auth(request)
        campaign = await _db.get_campaign(campaign_id, user_id)
        if not campaign:
            raise HTTPException(404, "Campaign not found")

        if not _ranker:
            raise HTTPException(503, "Resume ranking not configured. Set OPENAI_API_KEY.")

        if not _twilio:
            raise HTTPException(503, "Twilio not configured. Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN.")

        if not file.filename or not file.filename.lower().endswith(".zip"):
            raise HTTPException(400, "File must be a .zip archive containing resumes (PDF/DOCX)")

        zip_data = await file.read()
        if len(zip_data) > 50 * 1024 * 1024:
            raise HTTPException(400, "ZIP file too large (max 50 MB)")

        # Use job_description from form, or fall back to campaign's description
        jd = job_description.strip() or campaign.get("description", "") or campaign["job_role"]

        # Update campaign description if provided
        if job_description.strip() and job_description.strip() != campaign.get("description", ""):
            async with _db._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE campaigns SET description = $1, updated_at = NOW() WHERE id = $2",
                    job_description.strip(), campaign_id,
                )

        # 1. Parse resumes from ZIP
        parsed = parse_resumes_from_zip(zip_data)
        if not parsed:
            raise HTTPException(400, "No valid resumes found in ZIP (supported: PDF, DOCX, TXT)")

        valid_resumes = [r for r in parsed if r.get("text") and not r.get("error")]
        if not valid_resumes:
            raise HTTPException(400, f"Could not extract text from any of the {len(parsed)} files")

        # 2. Build job description and rank
        job_desc = f"Job Role: {campaign['job_role']}\n\n{jd}"
        if campaign.get("custom_prompt"):
            job_desc += f"\n\nAdditional Requirements:\n{campaign['custom_prompt']}"

        top_percent = _settings.ats_top_percent
        ranking_result = await _ranker.rank_resumes(
            resumes=valid_resumes,
            job_description=job_desc,
            top_percent=top_percent,
        )

        # 3. Store rankings in DB
        await _db.clear_resume_rankings(campaign_id, user_id)
        selected_set = {r["filename"] for r in ranking_result["selected"]}

        for r in ranking_result["all_ranked"]:
            await _db.add_resume_ranking(
                campaign_id=campaign_id,
                user_id=user_id,
                filename=r.get("filename", ""),
                full_name=r.get("full_name", ""),
                email=r.get("email", ""),
                phone=r.get("phone", ""),
                current_title=r.get("current_title", ""),
                years_experience=int(r.get("years_experience", 0)),
                resume_text=r.get("resume_text", ""),
                skills_match=r.get("skills_match", 0),
                experience_relevance=r.get("experience_relevance", 0),
                education_fit=r.get("education_fit", 0),
                overall_suitability=r.get("overall_suitability", 0),
                total_score=r.get("total_score", 0),
                reasoning=r.get("reasoning", ""),
                selected=r["filename"] in selected_set,
            )

        # 4. Auto-promote selected candidates with phone numbers
        selected_rankings = await _db.get_resume_rankings(campaign_id, user_id, selected_only=True)
        promoted = []
        skipped_no_phone = 0

        for r in selected_rankings:
            phone_raw = r.get("phone", "").strip()
            if not phone_raw:
                skipped_no_phone += 1
                continue

            e164, valid = normalise_phone(phone_raw)
            if not valid:
                skipped_no_phone += 1
                continue

            name_parts = (r.get("full_name", "") or "").strip().split(maxsplit=1)
            first_name = name_parts[0] if name_parts else ""
            last_name = name_parts[1] if len(name_parts) > 1 else ""

            promoted.append({
                "unique_record_id": f"resume_{r['id']}_{uuid.uuid4().hex[:6]}",
                "first_name": first_name,
                "last_name": last_name,
                "phone_e164": e164,
                "email": r.get("email", ""),
            })

        candidates_added = 0
        if promoted:
            candidates_added = await _db.add_candidates(campaign_id, user_id, promoted)
            promoted_ids = [r["id"] for r in selected_rankings if r.get("phone", "").strip()]
            if promoted_ids:
                await _db.mark_rankings_promoted(campaign_id, user_id, promoted_ids)

        parse_errors = [r for r in parsed if r.get("error")]

        # Check if user has a phone number for the next step
        user_phones = await _db.get_phone_numbers(user_id)
        has_phone = bool(user_phones)

        next_step = (
            "Buy a phone number, then start calls."
            if not has_phone
            else "Ready to start calls!"
        )

        return {
            "status": "ok",
            "rankings": {
                "total_resumes": ranking_result["stats"]["total_resumes"],
                "selected": ranking_result["stats"]["selected_count"],
                "avg_score": ranking_result["stats"]["avg_score"],
                "cutoff_score": ranking_result["stats"]["cutoff_score"],
            },
            "candidates_promoted": candidates_added,
            "skipped_no_phone": skipped_no_phone,
            "has_phone_number": has_phone,
            "parse_errors": [{"filename": e["filename"], "error": e["error"]} for e in parse_errors[:10]],
            "message": (
                f"Ranked {ranking_result['stats']['total_resumes']} resumes. "
                f"{candidates_added} candidates promoted to calls. "
                + next_step
            ),
        }

    @app.post("/api/campaigns/{campaign_id}/start")
    async def start_campaign_calls(campaign_id: int, request: Request):
        user_id = _auth.require_auth(request)
        campaign = await _db.get_campaign(campaign_id, user_id)
        if not campaign:
            raise HTTPException(404, "Campaign not found")

        # ── Require a purchased phone number ──
        user_phones = await _db.get_phone_numbers(user_id)
        if not user_phones:
            raise HTTPException(
                400,
                "You need to buy a phone number first. "
                "Go to the Phone Numbers tab to purchase one.",
            )

        # Use the first active phone number
        from_number = user_phones[0]["phone_e164"]

        # Check usage limits
        can_call = await _db.can_place_call(user_id)
        if not can_call:
            raise HTTPException(
                402,
                "Monthly call limit reached. Upgrade your plan for more calls.",
            )

        # Check if already running
        if campaign_id in _active_tasks and not _active_tasks[campaign_id].done():
            return {"status": "already_running"}

        # Start calling in background
        _active_tasks[campaign_id] = asyncio.create_task(
            _auto_start_calls(campaign_id, user_id, from_number, campaign)
        )

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
    #  Phone Number Management routes
    # ═══════════════════════════════════════════════════════════

    @app.get("/api/phone-numbers")
    async def list_phone_numbers(request: Request):
        """List all phone numbers owned by the user."""
        user_id = _auth.require_auth(request)
        db = _require_db()
        numbers = await db.get_phone_numbers(user_id)
        # Convert Decimal to float for JSON
        for n in numbers:
            n["monthly_cost"] = float(n.get("monthly_cost", 0))
            n["our_price"] = float(n.get("our_price", 0))
        return {"phone_numbers": numbers}

    @app.post("/api/phone-numbers/search")
    async def search_phone_numbers(request: Request):
        """Search available Twilio numbers by country/area code."""
        user_id = _auth.require_auth(request)
        if not _twilio:
            raise HTTPException(503, "Phone number purchasing not configured. Set TWILIO_ACCOUNT_SID.")

        body = await request.json()
        country = body.get("country_code", "US").upper()
        area_code = body.get("area_code", "")
        contains = body.get("contains", "")
        number_type = body.get("number_type", "Local")
        limit = min(int(body.get("limit", 20)), 30)

        try:
            numbers = await _twilio.search_available_numbers(
                country_code=country,
                area_code=area_code,
                contains=contains,
                number_type=number_type,
                limit=limit,
            )
            return {"numbers": numbers, "country": country}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/phone-numbers/purchase")
    async def purchase_phone_number(request: Request):
        """Create a Stripe Checkout session for phone number purchase.
        Actual Twilio purchase happens in the Stripe webhook after payment."""
        user_id = _auth.require_auth(request)
        db = _require_db()
        if not _twilio:
            raise HTTPException(503, "Phone number purchasing not configured.")
        if not _billing:
            raise HTTPException(503, "Billing not configured. Set STRIPE_SECRET_KEY.")

        # Enforce phone number limit per plan
        user = await _db.get_user_by_id(user_id)
        user_plan = user.get("plan", "free")
        max_phones = PLANS.get(user_plan, PLANS["free"]).get("max_phones", 1)
        current_phones = await _db.get_phone_numbers(user_id)
        if len(current_phones) >= max_phones:
            raise HTTPException(
                402,
                f"Your {PLANS[user_plan]['name']} plan allows up to {max_phones} phone number(s). Upgrade to add more.",
            )

        body = await request.json()
        phone_number = body.get("phone_number", "").strip()
        country_code = body.get("country_code", "US").upper()
        twilio_price = float(body.get("twilio_price", body.get("telnyx_price", 1.00)))
        our_price = float(body.get("our_price", 1.50))

        if not phone_number or not phone_number.startswith("+"):
            raise HTTPException(400, "Valid E.164 phone number required (e.g. +12025551234)")

        # Get or create Stripe customer
        user = await _db.get_user_by_id(user_id)
        customer_id = user.get("stripe_customer_id", "")
        if not customer_id:
            customer_id = await _billing.get_or_create_customer(
                user_id, user["email"], user.get("name", "")
            )
            await _db.update_user_stripe(user_id, customer_id)

        try:
            price_cents = int(our_price * 100)
            checkout_url = await _billing.create_phone_checkout_session(
                customer_id=customer_id,
                user_id=user_id,
                phone_number=phone_number,
                country_code=country_code,
                price_cents=price_cents,
                twilio_price=twilio_price,
            )
            return {"status": "checkout", "url": checkout_url}
        except Exception as e:
            log.error("phone_checkout_failed", phone=phone_number, error=str(e))
            raise HTTPException(500, f"Could not create checkout session: {e}")

    @app.delete("/api/phone-numbers/{phone_id}")
    async def release_phone_number(phone_id: int, request: Request):
        """Release a phone number — removes from Twilio and our DB."""
        user_id = _auth.require_auth(request)
        db = _require_db()
        if not _twilio:
            raise HTTPException(503, "Phone number service not configured.")

        record = await db.get_phone_number(phone_id, user_id)
        if not record:
            raise HTTPException(404, "Phone number not found")

        # Release from Twilio (telnyx_id column stores the Twilio SID)
        await _twilio.release_number(record["telnyx_id"])

        # Mark released in our DB
        await db.release_phone_number(phone_id, user_id)

        return {"status": "released", "message": f"Phone number {record['phone_e164']} released."}

    # ═══════════════════════════════════════════════════════════
    #  Billing routes
    # ═══════════════════════════════════════════════════════════
    @app.post("/api/billing/checkout")
    async def billing_checkout(request: Request):
        user_id = _auth.require_auth(request)
        if not _billing:
            raise HTTPException(503, "Billing not configured")

        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        plan = body.get("plan", "starter")
        if plan not in ("starter", "pro", "enterprise"):
            raise HTTPException(400, "Invalid plan")

        user = await _db.get_user_by_id(user_id)
        customer_id = user.get("stripe_customer_id", "")
        if not customer_id:
            customer_id = await _billing.get_or_create_customer(
                user_id, user["email"], user.get("name", "")
            )
            await _db.update_user_stripe(user_id, customer_id)

        url = await _billing.create_checkout_session(customer_id, user_id, plan)
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
            metadata = session.get("metadata", {})
            user_id = int(metadata.get("user_id", 0))
            session_type = metadata.get("type", "")
            customer_id = session.get("customer", "")

            if session_type == "phone_purchase" and user_id:
                # ── Phone number purchase fulfillment ──
                phone_number = metadata.get("phone_number", "")
                country_code = metadata.get("country_code", "US")
                twilio_price = float(metadata.get("twilio_price", 1.00))
                our_price = float(metadata.get("our_price", 1.50))
                payment_intent = session.get("payment_intent", "")

                log.info("phone_purchase_paid", user_id=user_id, phone=phone_number,
                         payment_intent=payment_intent)

                try:
                    # Purchase the number from Twilio now that payment succeeded
                    twilio_data = await _twilio.purchase_number(phone_number)
                    twilio_sid = twilio_data.get("sid", "")
                    friendly_name = twilio_data.get("friendly_name", phone_number)
                    capabilities = twilio_data.get("capabilities", {})

                    # Store in our database
                    await _db.add_phone_number(
                        user_id=user_id,
                        phone_e164=phone_number,
                        friendly_name=friendly_name,
                        country_code=country_code,
                        telnyx_id=twilio_sid,
                        vapi_phone_id=twilio_sid,
                        monthly_cost=twilio_price,
                        our_price=our_price,
                        capabilities=capabilities,
                    )

                    log.info("phone_number_purchased_via_stripe",
                             user_id=user_id, phone=phone_number,
                             twilio_sid=twilio_sid)
                except Exception as e:
                    # Payment succeeded but Twilio purchase failed —
                    # log for manual resolution / refund
                    log.error("twilio_purchase_after_payment_failed",
                              user_id=user_id, phone=phone_number,
                              payment_intent=payment_intent, error=str(e))

            elif user_id:
                # ── Paid plan subscription ──
                subscription_id = session.get("subscription", "")
                plan = metadata.get("plan", "starter")
                if plan not in PLANS:
                    plan = "starter"

                await _db.update_user_plan(
                    user_id=user_id,
                    plan=plan,
                    monthly_call_limit=PLANS[plan]["calls"],
                    stripe_customer_id=customer_id,
                    stripe_subscription_id=subscription_id,
                )
                log.info("user_upgraded", user_id=user_id, plan=plan)

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
    #  WebSocket Media Stream (Twilio ↔ OpenAI Realtime)
    # ═══════════════════════════════════════════════════════════
    @app.websocket("/ws/media-stream")
    async def media_stream_ws(websocket: WebSocket):
        await handle_media_stream(websocket, _settings, db=_db)

    # ═══════════════════════════════════════════════════════════
    #  Twilio Webhooks (call results)
    # ═══════════════════════════════════════════════════════════
    @app.post("/webhook/twilio/voice")
    async def twilio_voice_webhook(request: Request):
        """Twilio voice webhook — returns TwiML when call connects."""
        from app.webhook import handle_twilio_voice
        twiml = await handle_twilio_voice(request, _db, _settings)
        return Response(content=twiml, media_type="application/xml")

    @app.post("/webhook/twilio/status")
    async def twilio_status_webhook(request: Request):
        """Twilio status callback — processes call completion."""
        form = await request.form()
        call_sid = str(form.get("CallSid", ""))
        call_status = str(form.get("CallStatus", ""))
        duration = str(form.get("CallDuration", "0"))
        record_id = request.query_params.get("record_id", "")

        log.info("twilio_status_callback", call_sid=call_sid, status=call_status, record_id=record_id)

        if call_status != "completed":
            disposition = _TWILIO_STATUS_MAP.get(call_status)
            if disposition and record_id:
                candidate = await _db.get_candidate_by_record_id(record_id)
                if candidate:
                    await _db.update_call_result(
                        vapi_call_id=call_sid,
                        status=disposition.value,
                        short_summary=f"Call {call_status}",
                        raw_call_outcome=call_status,
                    )
            return {"ok": True}

        # Call completed — analyse with OpenAI
        if record_id:
            candidate = await _db.get_candidate_by_record_id(record_id)
            if candidate:
                recording_url = ""
                transcript = ""
                analysis = {}

                # Get recording
                try:
                    if _twilio:
                        recording_url = await _twilio.get_recording_url(call_sid)
                except Exception as e:
                    log.error("recording_fetch_failed", error=str(e))

                # Fetch transcript from DB (stored by media_stream.py)
                # Retry a few times since the WebSocket may still be storing it
                for _attempt in range(5):
                    cand = await _db.get_candidate_by_call_id(call_sid)
                    if cand and cand.get("transcript"):
                        transcript = cand["transcript"]
                        break
                    await asyncio.sleep(1.5)

                if not transcript:
                    log.warning("no_transcript_found", call_sid=call_sid, record_id=record_id)

                # Analyse with OpenAI if we have a transcript
                if transcript and _settings.openai_api_key:
                    try:
                        analysis = await analyse_transcript_with_openai(
                            transcript, _settings.openai_api_key
                        )
                    except Exception as e:
                        log.error("analysis_failed", error=str(e))

                disp_str = analysis.get("disposition", "").strip().upper()
                disposition = None
                if disp_str:
                    try:
                        from app.models import Disposition as Disp
                        disposition = Disp(disp_str)
                    except ValueError:
                        pass
                if not disposition:
                    from app.models import Disposition as Disp
                    disposition = _parse_disposition_from_text(analysis.get("summary", ""))

                summary = analysis.get("summary", f"Call completed, duration {duration}s")
                disposition = _cross_check_disposition(disposition, summary)

                await _db.update_call_result(
                    vapi_call_id=call_sid,
                    status=disposition.value,
                    short_summary=summary,
                    raw_call_outcome=call_status,
                    transcript=transcript,
                    recording_url=recording_url,
                    extracted_location=analysis.get("location", ""),
                    extracted_availability=analysis.get("availability", ""),
                )

                log.info("call_result_saved", call_sid=call_sid, disposition=disposition.value)

        return {"ok": True}

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

        e164, valid = normalise_phone(phone)
        if not valid:
            raise HTTPException(400, f"Invalid phone number: {phone}")

        if not _twilio:
            raise HTTPException(503, "Twilio not configured")

        try:
            # Use the user's first phone number or default
            user_phones = await _db.get_phone_numbers(user_id)
            from_number = _settings.twilio_phone_number
            if user_phones:
                from_number = user_phones[0]["phone_e164"]

            result = await _twilio.place_call(
                phone_e164=e164,
                from_number=from_number,
                candidate_name=name,
                record_id="test-debug",
                job_role="Test Call",
            )
            return {"status": "ok", "twilio_response": result}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    # ═══════════════════════════════════════════════════════════
    #  Debug / Admin (temporary)
    # ═══════════════════════════════════════════════════════════
    @app.get("/debug/campaigns")
    async def debug_campaigns():
        """Show all campaigns + candidates (temp debug — remove later)."""
        db = _require_db()
        async with db._pool.acquire() as conn:
            campaigns = await conn.fetch("SELECT id, user_id, name, status, total_candidates, vapi_assistant_id FROM campaigns ORDER BY id")
            candidates = await conn.fetch("SELECT id, campaign_id, first_name, phone_e164, status, vapi_call_id, attempt_count, last_called_at FROM candidates ORDER BY id")
            users = await conn.fetch("SELECT id, email, name FROM users ORDER BY id")
        return {
            "users": [dict(r) for r in users],
            "campaigns": [dict(r) for r in campaigns],
            "candidates": [dict(r) for r in candidates],
        }

    @app.get("/debug/call/{call_sid}")
    async def debug_call(call_sid: str):
        """Check Twilio call status (temp debug — remove later)."""
        try:
            if not _twilio:
                return {"error": "Twilio not configured"}
            data = await _twilio.get_call(call_sid)
            return data
        except Exception as e:
            return {"error": str(e)}

    @app.post("/debug/fix-candidate/{candidate_id}")
    async def debug_fix_candidate(candidate_id: int, request: Request):
        """Fix candidate phone and reset for retry (temp debug)."""
        body = await request.json()
        phone = body.get("phone", "")
        db = _require_db()
        async with db._pool.acquire() as conn:
            await conn.execute(
                """UPDATE candidates SET phone_e164 = $1, status = 'PENDING',
                   vapi_call_id = '', attempt_count = 0, last_called_at = NULL
                   WHERE id = $2""",
                phone, candidate_id,
            )
            # Also reset campaign status to draft so we can re-start
            await conn.execute(
                """UPDATE campaigns SET status = 'draft'
                   WHERE id = (SELECT campaign_id FROM candidates WHERE id = $1)""",
                candidate_id,
            )
        return {"ok": True, "candidate_id": candidate_id, "new_phone": phone}

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
