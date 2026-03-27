"""
WebSocket Media Stream handler — bridges Twilio Media Streams to OpenAI Realtime API.

Flow:
  1. Twilio places call → callee answers → TwiML <Connect><Stream> opens WebSocket here
  2. Twilio sends audio as base64 g711_ulaw 8kHz mono
  3. We forward audio to OpenAI Realtime API (also g711_ulaw)
  4. OpenAI generates AI voice response → we stream it back to Twilio
  5. Conversation transcript is captured and stored for post-call analysis
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Optional

import structlog
from fastapi import WebSocket, WebSocketDisconnect

from app.config import Settings
from app.webhook import RECRUITMENT_SYSTEM_PROMPT, CAMPAIGN_SCREENING_PROMPT

log = structlog.get_logger(__name__)

OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17"

# Voice for the AI assistant
VOICE = "alloy"


def generate_screening_questions(job_role: str, job_description: str) -> str:
    """
    Generate skill-based screening questions from the job description.
    Extracts key skills/requirements and creates conversational questions
    about the candidate's experience with each.
    """
    questions = []

    # Always ask about interest
    questions.append(
        f'   a. "Are you currently open to new opportunities, specifically for a {job_role} role?"'
    )

    # Parse skills/requirements from job description
    skills = _extract_skills_from_description(job_description)

    if skills:
        # Ask about experience with the top required skills (max 3 to keep call short)
        for i, skill in enumerate(skills[:3], start=1):
            letter = chr(ord('a') + i)  # b, c, d
            questions.append(
                f'   {letter}. "Could you tell me about your experience with {skill}? '
                f'How many years have you worked with it?"'
            )
        next_letter = chr(ord('a') + min(len(skills), 3) + 1)
    else:
        # Generic experience question if no specific skills found
        questions.append(
            f'   b. "Could you briefly tell me about your experience related to {job_role}?"'
        )
        next_letter = 'c'

    # Location preference
    questions.append(
        f'   {next_letter}. "What location or work arrangement are you looking for — on-site, remote, or hybrid?"'
    )

    # Availability
    next_letter = chr(ord(next_letter) + 1)
    questions.append(
        f'   {next_letter}. "If things moved forward, how soon could you start or what would your notice period be?"'
    )

    return "\n".join(questions)


def _extract_skills_from_description(description: str) -> list[str]:
    """
    Extract key skills and technologies from a job description.
    Returns a list of skill names found.
    """
    if not description or len(description.strip()) < 20:
        return []

    import re

    desc_lower = description.lower()
    found_skills = []

    # Common tech skills / frameworks / tools to look for
    skill_patterns = [
        # Programming languages
        r'\bpython\b', r'\bjava(?:script)?\b', r'\btypescript\b', r'\bc\+\+\b',
        r'\bc#\b', r'\bruby\b', r'\bgo(?:lang)?\b', r'\brust\b', r'\bswift\b',
        r'\bkotlin\b', r'\bphp\b', r'\bscala\b', r'\br\b(?=\s|,|\.|/)',
        # Web frameworks
        r'\breact(?:\.?js)?\b', r'\bangular\b', r'\bvue(?:\.?js)?\b', r'\bnode(?:\.?js)?\b',
        r'\bdjango\b', r'\bflask\b', r'\bfastapi\b', r'\bspring\b',
        r'\b\.net\b', r'\brails\b', r'\bnext\.?js\b',
        # Cloud & DevOps
        r'\baws\b', r'\bazure\b', r'\bgcp\b', r'\bgoogle cloud\b',
        r'\bdocker\b', r'\bkubernetes\b', r'\bterraform\b', r'\bci/cd\b',
        r'\bjenkins\b', r'\bgit(?:hub)?\b',
        # Data & ML
        r'\bmachine learning\b', r'\bdeep learning\b', r'\bdata science\b',
        r'\btensorflow\b', r'\bpytorch\b', r'\bsql\b', r'\bnosql\b',
        r'\bmongodb\b', r'\bpostgresql?\b', r'\belasticsearch\b',
        r'\bspark\b', r'\bhadoop\b', r'\bpandas\b',
        # General skills
        r'\bproject management\b', r'\bagile\b', r'\bscrum\b',
        r'\bleadership\b', r'\bcommunication skills?\b',
        r'\bsales\b', r'\bmarketing\b', r'\baccount management\b',
        r'\bcustomer service\b', r'\bnegotiation\b',
        r'\bfinancial analysis\b', r'\baccounting\b', r'\bbookkeeping\b',
        r'\bexcel\b', r'\btableau\b', r'\bpower bi\b',
        # Design
        r'\bfigma\b', r'\bui/?ux\b', r'\badobe\b', r'\bphotoshop\b',
        # Certifications / methodologies
        r'\bpmp\b', r'\bcpa\b', r'\bsix sigma\b', r'\bitil\b',
    ]

    for pattern in skill_patterns:
        match = re.search(pattern, desc_lower)
        if match:
            # Clean up the matched skill name for display
            skill = match.group(0).strip()
            # Title-case common acronyms
            upper_skills = {'aws', 'gcp', 'sql', 'nosql', 'ci/cd', 'pmp', 'cpa', 'ui/ux'}
            if skill in upper_skills:
                skill = skill.upper()
            elif len(skill) <= 4 and skill.isalpha():
                skill = skill.upper() if skill in {'php', 'css', 'html', 'api'} else skill.capitalize()
            else:
                skill = skill.title()
            # Avoid duplicates (case-insensitive)
            if not any(s.lower() == skill.lower() for s in found_skills):
                found_skills.append(skill)

    # Also look for "experience with/in X" patterns in the description
    exp_patterns = [
        r'experience (?:with|in|using) ([A-Za-z][A-Za-z\s/+#.]{1,30}?)(?:\.|,|\band\b|\bor\b|\n|$)',
        r'proficien(?:t|cy) (?:in|with) ([A-Za-z][A-Za-z\s/+#.]{1,30}?)(?:\.|,|\band\b|\bor\b|\n|$)',
        r'knowledge of ([A-Za-z][A-Za-z\s/+#.]{1,30}?)(?:\.|,|\band\b|\bor\b|\n|$)',
    ]
    for pattern in exp_patterns:
        for match in re.finditer(pattern, description, re.IGNORECASE):
            skill = match.group(1).strip().rstrip('.')
            if 2 < len(skill) < 30 and not any(s.lower() == skill.lower() for s in found_skills):
                found_skills.append(skill)

    return found_skills[:6]  # Cap at 6 skills max


async def handle_media_stream(
    websocket: WebSocket,
    settings: Settings,
    db=None,
):
    """
    Handle a Twilio Media Stream WebSocket connection.

    Bridges audio between Twilio and OpenAI Realtime API for real-time
    AI-powered voice conversation.
    """
    await websocket.accept()

    openai_api_key = settings.openai_api_key
    if not openai_api_key:
        log.error("media_stream_no_openai_key")
        await websocket.close(code=1011, reason="OpenAI API key not configured")
        return

    # State
    stream_sid: Optional[str] = None
    call_sid: Optional[str] = None
    candidate_name = "there"
    job_role = ""
    campaign_id = ""
    transcript_parts: list[str] = []  # Collect transcript fragments
    openai_ws = None

    try:
        import websockets

        # Connect to OpenAI Realtime API
        openai_ws = await websockets.connect(
            OPENAI_REALTIME_URL,
            additional_headers={
                "Authorization": f"Bearer {openai_api_key}",
                "OpenAI-Beta": "realtime=v1",
            },
        )
        log.info("openai_realtime_connected")

        async def send_session_update():
            """Configure the OpenAI Realtime session with our system prompt."""
            job_description = ""
            custom_prompt = ""

            # Fetch campaign details from DB if campaign_id is available
            if campaign_id and db:
                try:
                    campaign_data = await _fetch_campaign(db, campaign_id)
                    if campaign_data:
                        job_description = campaign_data.get("description", "") or ""
                        custom_prompt = campaign_data.get("custom_prompt", "") or ""
                        if not job_role and campaign_data.get("job_role"):
                            pass  # job_role is nonlocal, don't reassign here
                except Exception as e:
                    log.warning("campaign_fetch_failed", campaign_id=campaign_id, error=str(e))

            # Use campaign-aware prompt if we have a job description
            if job_role and job_description:
                screening_questions = generate_screening_questions(job_role, job_description)
                system_prompt = CAMPAIGN_SCREENING_PROMPT.format(
                    job_role=job_role,
                    job_description=job_description,
                    screening_questions=screening_questions,
                )
                system_prompt = system_prompt.replace("{first_name}", candidate_name)
                if custom_prompt:
                    system_prompt += f"\n\nADDITIONAL INSTRUCTIONS FROM THE RECRUITER:\n{custom_prompt}"
            else:
                system_prompt = RECRUITMENT_SYSTEM_PROMPT.replace(
                    "{first_name}", candidate_name
                )
                if job_role:
                    system_prompt = (
                        f"You are screening for the role: {job_role}\n\n" + system_prompt
                    )

            session_config = {
                "type": "session.update",
                "session": {
                    "turn_detection": {"type": "server_vad"},
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
                    "voice": VOICE,
                    "instructions": system_prompt,
                    "modalities": ["text", "audio"],
                    "temperature": 0.8,
                    "input_audio_transcription": {
                        "model": "whisper-1",
                    },
                },
            }
            await openai_ws.send(json.dumps(session_config))
            log.info(
                "session_update_sent",
                candidate=candidate_name,
                job_role=job_role,
            )

        async def receive_from_twilio():
            """Read messages from Twilio and forward audio to OpenAI."""
            nonlocal stream_sid, call_sid, candidate_name, job_role

            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    event_type = data.get("event")

                    if event_type == "start":
                        start_data = data.get("start", {})
                        stream_sid = start_data.get("streamSid")
                        call_sid = start_data.get("callSid")

                        # Extract custom parameters passed via TwiML
                        custom_params = start_data.get("customParameters", {})
                        candidate_name = custom_params.get(
                            "candidate_name", "there"
                        )
                        job_role = custom_params.get("job_role", "")
                        campaign_id = custom_params.get("campaign_id", "")

                        log.info(
                            "twilio_stream_started",
                            stream_sid=stream_sid,
                            call_sid=call_sid,
                            candidate=candidate_name,
                        )

                        # Now configure OpenAI session with candidate context
                        await send_session_update()

                    elif event_type == "media":
                        # Forward audio to OpenAI
                        audio_payload = data.get("media", {}).get("payload", "")
                        if audio_payload and openai_ws:
                            audio_append = {
                                "type": "input_audio_buffer.append",
                                "audio": audio_payload,
                            }
                            await openai_ws.send(json.dumps(audio_append))

                    elif event_type == "stop":
                        log.info(
                            "twilio_stream_stopped",
                            stream_sid=stream_sid,
                            call_sid=call_sid,
                        )
                        break

            except WebSocketDisconnect:
                log.info("twilio_ws_disconnected", call_sid=call_sid)
            except Exception as e:
                log.error("twilio_receive_error", error=str(e))

        async def receive_from_openai():
            """Read messages from OpenAI and forward audio back to Twilio."""
            nonlocal transcript_parts

            try:
                async for message in openai_ws:
                    data = json.loads(message)
                    event_type = data.get("type", "")

                    if event_type == "response.audio.delta":
                        # Stream AI audio back to Twilio
                        audio_delta = data.get("delta", "")
                        if audio_delta and stream_sid:
                            twilio_msg = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": audio_delta},
                            }
                            await websocket.send_json(twilio_msg)

                    elif event_type == "response.audio_transcript.delta":
                        # AI's spoken text (partial)
                        delta = data.get("delta", "")
                        if delta:
                            transcript_parts.append(f"AI: {delta}")

                    elif event_type == "response.audio_transcript.done":
                        # AI finished a complete utterance
                        full_text = data.get("transcript", "")
                        if full_text:
                            # Replace partial deltas with the final version
                            # Remove prior AI partial entries for this response
                            transcript_parts = [
                                p
                                for p in transcript_parts
                                if not p.startswith("AI: ")
                                or p == f"AI: {full_text}"
                            ]
                            if f"AI: {full_text}" not in transcript_parts:
                                transcript_parts.append(f"AI: {full_text}")
                            log.debug("ai_utterance", text=full_text[:80])

                    elif (
                        event_type
                        == "conversation.item.input_audio_transcription.completed"
                    ):
                        # User's speech transcription
                        user_text = data.get("transcript", "")
                        if user_text:
                            transcript_parts.append(f"User: {user_text}")
                            log.debug("user_utterance", text=user_text[:80])

                    elif event_type == "session.created":
                        log.info("openai_session_created")

                    elif event_type == "session.updated":
                        log.info("openai_session_updated")

                    elif event_type == "error":
                        error_data = data.get("error", {})
                        log.error(
                            "openai_realtime_error",
                            error_type=error_data.get("type"),
                            message=error_data.get("message"),
                        )

            except Exception as e:
                log.error("openai_receive_error", error=str(e))

        # Run both receivers concurrently
        await asyncio.gather(
            receive_from_twilio(),
            receive_from_openai(),
        )

    except ImportError:
        log.error("websockets_not_installed", msg="pip install websockets")
        await websocket.close(code=1011, reason="Server dependency missing")
        return
    except Exception as e:
        log.error("media_stream_error", error=str(e))
    finally:
        # Clean up OpenAI WebSocket
        if openai_ws:
            try:
                await openai_ws.close()
            except Exception:
                pass

        # Store transcript in database
        if call_sid and transcript_parts and db:
            full_transcript = "\n".join(transcript_parts)
            try:
                await _store_transcript(db, call_sid, full_transcript)
                log.info(
                    "transcript_stored",
                    call_sid=call_sid,
                    length=len(full_transcript),
                )
            except Exception as e:
                log.error("transcript_store_failed", error=str(e))

        log.info(
            "media_stream_ended",
            call_sid=call_sid,
            transcript_lines=len(transcript_parts),
        )


async def _store_transcript(db, call_sid: str, transcript: str) -> None:
    """
    Store the call transcript in the database.
    Works with both SQLite (Database) and PostgreSQL (SaaSDatabase).
    """
    # Try SQLite database (app/database.py)
    if hasattr(db, "_db") and db._db is not None:
        await db._db.execute(
            "UPDATE call_records SET transcript = ?, updated_at = datetime('now') WHERE vapi_call_id = ?",
            (transcript, call_sid),
        )
        await db._db.commit()
        return

    # Try PostgreSQL database (app/saas_db.py)
    if hasattr(db, "_pool") and db._pool is not None:
        async with db._pool.acquire() as conn:
            await conn.execute(
                "UPDATE candidates SET transcript = $1 WHERE vapi_call_id = $2",
                transcript, call_sid,
            )
        return


async def _fetch_campaign(db, campaign_id: str) -> Optional[dict]:
    """
    Fetch campaign details (job description, custom prompt) from the database.
    Works with both SQLite (Database) and PostgreSQL (SaaSDatabase).
    """
    if not campaign_id:
        return None

    # Try PostgreSQL database (app/saas_db.py)
    if hasattr(db, "_pool") and db._pool is not None:
        async with db._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT job_role, description, custom_prompt FROM campaigns WHERE id = $1",
                int(campaign_id),
            )
            if row:
                return dict(row)

    return None
