"""
Shared data models used across the application.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ── Disposition enum ────────────────────────────────────────────
class Disposition(str, enum.Enum):
    QUALIFIED = "QUALIFIED"
    PARTIALLY_QUALIFIED = "PARTIALLY_QUALIFIED"
    NOT_QUALIFIED = "NOT_QUALIFIED"
    ACTIVE_LOOKING = "ACTIVE_LOOKING"
    NOT_LOOKING = "NOT_LOOKING"
    CALL_BACK = "CALL_BACK"
    NO_ANSWER = "NO_ANSWER"
    WRONG_NUMBER = "WRONG_NUMBER"
    DNC = "DNC"
    VOICEMAIL = "VOICEMAIL"
    BUSY = "BUSY"
    FAILED = "FAILED"
    PENDING = "PENDING"


# ── Candidate record from CSV ──────────────────────────────────
class CandidateRecord(BaseModel):
    unique_record_id: str
    first_name: str = ""
    last_name: str = ""
    phone_raw: str = Field(..., description="Phone number as supplied in CSV")
    phone_e164: str = Field(default="", description="Normalised E.164 phone number")
    email: Optional[str] = None
    extra_fields: dict = Field(default_factory=dict, description="Any additional CSV columns")


# ── Call record (persisted in DB) ───────────────────────────────
class CallRecord(BaseModel):
    id: Optional[int] = None
    unique_record_id: str
    first_name: str = ""
    last_name: str = ""
    phone_e164: str
    vapi_call_id: str = ""
    job_role: str = ""
    status: Disposition = Disposition.PENDING
    short_summary: str = ""
    attempt_count: int = 0
    last_called_at: Optional[datetime] = None
    raw_call_outcome: str = ""
    transcript: str = ""
    recording_url: str = ""
    extracted_location: str = ""
    extracted_availability: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ── VAPI webhook payloads (subset we care about) ───────────────
class VAPICallEndPayload(BaseModel):
    """Minimal schema for the end-of-call webhook from VAPI."""
    call_id: str = Field(alias="id", default="")
    status: str = ""
    ended_reason: str = Field(alias="endedReason", default="")
    transcript: str = ""
    summary: str = ""
    recording_url: str = Field(alias="recordingUrl", default="")
    # analysis fields populated by VAPI
    analysis: Optional[dict] = None

    model_config = {"populate_by_name": True}
