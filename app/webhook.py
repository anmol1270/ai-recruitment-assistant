"""
Twilio webhook receiver for call events.
Handles:
  - Voice webhook (TwiML response when call connects → start OpenAI Realtime stream)
  - Status callback (call completed, failed, etc.)
  - Transcription / recording callbacks
  - Disposition analysis via OpenAI after call ends
"""

from __future__ import annotations

import json
from typing import Optional
from urllib.parse import parse_qs

import httpx
import structlog
from fastapi import FastAPI, Header, HTTPException, Request, Query
from fastapi.responses import Response

from app.config import Settings
from app.database import Database
from app.models import Disposition

log = structlog.get_logger(__name__)

# Map Twilio call status to our dispositions
_TWILIO_STATUS_MAP: dict[str, Disposition] = {
    "busy": Disposition.BUSY,
    "no-answer": Disposition.NO_ANSWER,
    "failed": Disposition.FAILED,
    "canceled": Disposition.FAILED,
}

# System prompt for recruitment screening
RECRUITMENT_SYSTEM_PROMPT = """You are a friendly, professional recruitment assistant calling on behalf of a recruitment agency.

Your goal is to have a SHORT qualifying screening call (under 3 minutes).

CONVERSATION FLOW:
1. Greet the candidate by first name. Introduce yourself: "Hi {first_name}, this is an AI assistant calling from the recruitment team. I hope I'm not catching you at a bad time?"
2. If they say it's a bad time, politely ask when would be better, note it, and end the call.
3. Ask the KEY QUESTION: "I'm reaching out because we have your profile on file. I just wanted to check — are you currently open to new opportunities, or are you actively looking for a new role?"
4. Based on their answer:
   - If ACTIVELY LOOKING or OPEN: Say "That's great to hear!" Then ask these qualifying questions one at a time:
     a. "Could you briefly tell me what kind of role you're looking for?"
     b. "What location or work arrangement works best for you — on-site, remote, or hybrid?"
     c. "Could you share a bit about your most relevant experience?"
     d. "If things moved forward, how soon could you start or what would your notice period be?"
   - If NOT LOOKING: Say "No problem at all. Thanks for letting me know. We'll update our records."
   - If they say WRONG NUMBER or they're not who we're looking for: Apologise and end politely.
5. After the questions, thank them: "Great, thank you for your time! Our team will review your responses and get back to you if there's a good fit."

RULES:
- Be concise and respectful of their time
- Ask questions ONE AT A TIME — wait for the answer before asking the next
- Do NOT pressure anyone
- If they ask to be removed from the list, confirm you'll do so immediately
- Keep the entire call under 3 minutes
- Speak naturally and conversationally
- If you detect voicemail, leave a brief message: "Hi {first_name}, this is a call from the recruitment team. We were checking if you're open to new opportunities. No need to call back — we may try again another time. Thanks!"
"""

CAMPAIGN_SCREENING_PROMPT = """You are a friendly, professional recruitment assistant calling on behalf of a recruitment agency.

You are conducting a qualifying screening call for the following role:
JOB ROLE: {job_role}

JOB DESCRIPTION:
{job_description}

Your goal is to have a SHORT qualifying screening call (under 3 minutes). Ask questions that help determine if the candidate is a good fit for this specific role.

CONVERSATION FLOW:
1. Greet the candidate by first name: "Hi {{first_name}}, this is an AI assistant calling from the recruitment team. I hope I'm not catching you at a bad time?"
2. If they say it's a bad time, politely ask when would be better, note it, and end the call.
3. Briefly mention the role: "We're currently looking for a {job_role} and your profile caught our attention. Are you open to hearing more?"
4. If they're interested, ask these qualifying screening questions one at a time:
{screening_questions}
5. After the questions, thank them: "Great, thank you for your time! Our team will review your responses and get back to you if there's a good fit."
6. If NOT interested: "No problem at all. Thanks for letting me know. We'll update our records."
7. If WRONG NUMBER: Apologise and end politely.

RULES:
- Be concise and respectful of their time
- Ask questions ONE AT A TIME — wait for the answer before asking the next
- Do NOT pressure anyone
- If they ask to be removed from the list, confirm you'll do so immediately
- Keep the entire call under 3 minutes
- Speak naturally and conversationally
- Use the job description to assess whether the candidate's experience is relevant
"""


def _parse_disposition_from_text(text: str) -> Disposition:
    """Infer disposition from transcript or summary text."""
    s = text.lower()
    if any(kw in s for kw in ["not looking", "not interested", "not open", "declined"]):
        return Disposition.NOT_LOOKING
    if any(kw in s for kw in ["actively looking", "open to", "interested in", "looking for"]):
        return Disposition.QUALIFIED
    if any(kw in s for kw in ["call back", "callback", "busy", "bad time"]):
        return Disposition.CALL_BACK
    if any(kw in s for kw in ["wrong number", "wrong person"]):
        return Disposition.WRONG_NUMBER
    if any(kw in s for kw in ["remove", "do not call", "unsubscribe"]):
        return Disposition.DNC
    return Disposition.NOT_QUALIFIED


def _cross_check_disposition(disposition: Disposition, summary: str) -> Disposition:
    """Cross-check disposition against the summary text."""
    if not summary:
        return disposition

    s = summary.lower()

    # Map ACTIVE_LOOKING → QUALIFIED (person looking for a job = qualified)
    if disposition == Disposition.ACTIVE_LOOKING:
        disposition = Disposition.QUALIFIED

    if disposition == Disposition.QUALIFIED:
        not_looking_signals = [
            "not looking", "not interested", "not open",
            "not currently looking", "not actively looking",
            "declined", "not seeking", "happy where",
        ]
        if any(kw in s for kw in not_looking_signals):
            log.warning("disposition_cross_check_corrected", original="QUALIFIED", corrected="NOT_LOOKING")
            return Disposition.NOT_LOOKING

    if disposition == Disposition.NOT_LOOKING:
        active_signals = [
            "actively looking for", "is looking for a new",
            "interested in new opportunities",
            "open to new roles", "seeking new",
        ]
        if any(kw in s for kw in active_signals) and not any(
            neg in s for neg in ["not looking", "not interested", "not open", "declined"]
        ):
            log.warning("disposition_cross_check_corrected", original="NOT_LOOKING", corrected="QUALIFIED")
            return Disposition.QUALIFIED

    return disposition


async def analyse_transcript_with_openai(
    transcript: str,
    openai_api_key: str,
) -> dict:
    """
    Send the call transcript to OpenAI for disposition analysis.
    Returns structured analysis: disposition, summary, location, availability.
    """
    if not transcript or not openai_api_key:
        return {}

    analysis_prompt = """Analyze this recruitment call transcript and extract:

1. disposition: Choose EXACTLY ONE:
   - QUALIFIED: Candidate is interested/looking for a job AND has relevant experience. If a person says they are looking for a job or open to opportunities, mark them as QUALIFIED.
   - PARTIALLY_QUALIFIED: Candidate is interested but may lack some required experience
   - NOT_QUALIFIED: Candidate lacks required experience or is clearly not a fit
   - NOT_LOOKING: Candidate is NOT looking or NOT interested in opportunities
   - CALL_BACK: Candidate asked to call back later or said it's a bad time
   - WRONG_NUMBER: Wrong person or number
   - DNC: Asked to be removed from call list

IMPORTANT RULES:
- If the candidate says they ARE looking for a job, ARE open to opportunities, or ARE interested → disposition MUST be QUALIFIED (not ACTIVE_LOOKING)
- Pay close attention to negation. "I am NOT looking" = NOT_LOOKING
- If screening questions about specific skills were asked, factor the candidate's experience into the disposition

2. summary: A detailed 2-3 sentence summary of the call outcome. Include: whether the candidate is interested, their relevant experience/skills discussed, preferred location/work arrangement, and availability. This summary will be shown to the recruiter.
3. location: Any location or work arrangement mentioned (empty string if none)
4. availability: Any availability/timeline/notice period mentioned (empty string if none)
5. screening_answers: A brief summary of the candidate's answers to any screening questions asked (skills, experience, years of experience). Empty string if no screening questions were asked.

Return ONLY valid JSON:
{"disposition": "...", "summary": "...", "location": "...", "availability": "...", "screening_answers": "..."}"""

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {openai_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": analysis_prompt},
                    {"role": "user", "content": f"TRANSCRIPT:\n{transcript}"},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)


def generate_voice_twiml(
    candidate_name: str = "there",
    job_role: str = "",
    settings: Optional[Settings] = None,
    campaign_id: str = "",
) -> str:
    """
    Generate TwiML that connects the call to OpenAI Realtime API
    via Twilio Media Streams for a real-time AI voice conversation.
    """
    webhook_url = settings.webhook_base_url if settings else ""
    openai_api_key = settings.openai_api_key if settings else ""

    if openai_api_key:
        # Use Twilio Media Streams → OpenAI Realtime for full AI conversation
        # Escape candidate_name and job_role for XML safety
        from xml.sax.saxutils import escape as xml_escape
        safe_name = xml_escape(candidate_name)
        safe_role = xml_escape(job_role)
        safe_campaign_id = xml_escape(campaign_id)

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{webhook_url.replace('https://', '').replace('http://', '')}/ws/media-stream">
            <Parameter name="candidate_name" value="{safe_name}" />
            <Parameter name="job_role" value="{safe_role}" />
            <Parameter name="campaign_id" value="{safe_campaign_id}" />
        </Stream>
    </Connect>
</Response>"""
    else:
        # Fallback: simple TwiML with speech
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">
        Hi {candidate_name}, this is an AI assistant calling from the recruitment team.
        Are you currently open to new opportunities?
    </Say>
    <Gather input="speech" timeout="5" speechTimeout="auto"
            action="{webhook_url}/webhook/twilio/gather?candidate_name={candidate_name}">
        <Say voice="Polly.Joanna">Please go ahead, I'm listening.</Say>
    </Gather>
    <Say voice="Polly.Joanna">
        I didn't catch that. We'll try again another time. Thanks for your time!
    </Say>
</Response>"""

    return twiml


async def handle_twilio_voice(
    request: Request,
    db: Database,
    settings: Settings,
) -> str:
    """Handle Twilio voice webhook when call connects. Returns TwiML."""
    form = await request.form()
    call_sid = form.get("CallSid", "")
    answered_by = form.get("AnsweredBy", "human")

    # Get context from query params
    candidate_name = request.query_params.get("candidate_name", "there")
    record_id = request.query_params.get("record_id", "")
    job_role = request.query_params.get("job_role", "")
    campaign_id = request.query_params.get("campaign_id", "")

    log.info(
        "twilio_voice_webhook",
        call_sid=call_sid,
        answered_by=answered_by,
        candidate=candidate_name,
    )

    # If voicemail detected, leave a message
    if answered_by in ("machine_start", "machine_end_beep", "machine_end_silence"):
        from xml.sax.saxutils import escape as xml_escape
        safe_name = xml_escape(candidate_name)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">
        Hi {safe_name}, this is the recruitment team. We were checking if you're open to new opportunities. No need to call back. Thanks!
    </Say>
</Response>"""
        # Update DB with voicemail disposition
        if record_id:
            record = await db.get_record_by_id(record_id)
            if record:
                await db.update_call_result(
                    vapi_call_id=call_sid,
                    status=Disposition.VOICEMAIL,
                    short_summary="Voicemail detected, left message",
                    raw_call_outcome=f"machine_detected:{answered_by}",
                )
        return twiml

    # Human answered — generate AI conversation TwiML
    return generate_voice_twiml(
        candidate_name=candidate_name,
        job_role=job_role,
        settings=settings,
        campaign_id=campaign_id,
    )


async def handle_twilio_status(
    request: Request,
    db: Database,
    settings: Settings,
) -> dict:
    """Handle Twilio status callback when call ends."""
    form = await request.form()
    call_sid = str(form.get("CallSid", ""))
    call_status = str(form.get("CallStatus", ""))
    duration = str(form.get("CallDuration", "0"))
    record_id = request.query_params.get("record_id", "")

    log.info(
        "twilio_status_callback",
        call_sid=call_sid,
        status=call_status,
        duration=duration,
        record_id=record_id,
    )

    if call_status != "completed":
        # Call didn't connect
        disposition = _TWILIO_STATUS_MAP.get(call_status, Disposition.FAILED)
        summary = f"Call {call_status}"

        if record_id:
            record = await db.get_record_by_id(record_id)
            if record:
                await db.update_call_result(
                    vapi_call_id=call_sid,
                    status=disposition,
                    short_summary=summary,
                    raw_call_outcome=call_status,
                )
        return {"ok": True}

    # Call completed — fetch transcript and analyse
    try:
        from app.twilio_service import TwilioService
        twilio = TwilioService(settings)

        # Get recording URL
        recording_url = await twilio.get_recording_url(call_sid)

        # For now, use the transcript from the media stream session
        # (stored during the WebSocket conversation)
        transcript = await db.get_call_transcript(call_sid) or ""

        # Analyse transcript with OpenAI
        analysis = {}
        if transcript and settings.openai_api_key:
            try:
                analysis = await analyse_transcript_with_openai(
                    transcript, settings.openai_api_key
                )
            except Exception as e:
                log.error("openai_analysis_failed", call_sid=call_sid, error=str(e))

        # Determine disposition
        disp_str = analysis.get("disposition", "").strip().upper()
        disposition = None
        if disp_str:
            try:
                disposition = Disposition(disp_str)
            except ValueError:
                pass

        if not disposition:
            disposition = _parse_disposition_from_text(
                analysis.get("summary", "") or transcript
            )

        summary = analysis.get("summary", "")
        if not summary:
            summary = f"Call completed, duration {duration}s"

        disposition = _cross_check_disposition(disposition, summary)

        # Update database
        if record_id:
            record = await db.get_record_by_id(record_id)
            if record:
                await db.update_call_result(
                    vapi_call_id=call_sid,
                    status=disposition,
                    short_summary=summary,
                    raw_call_outcome=call_status,
                    transcript=transcript,
                    recording_url=recording_url,
                    extracted_location=analysis.get("location", ""),
                    extracted_availability=analysis.get("availability", ""),
                )

        log.info(
            "call_result_saved",
            call_sid=call_sid,
            record_id=record_id,
            disposition=disposition.value,
            summary=summary[:100],
        )

        await twilio.close()
    except Exception as e:
        log.error("status_processing_error", call_sid=call_sid, error=str(e))

    return {"ok": True}
