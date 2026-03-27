"""
VAPI API client for creating/managing voice agents and placing outbound calls.
"""

from __future__ import annotations

import httpx
import structlog
from typing import Optional

from app.config import Settings

log = structlog.get_logger(__name__)

# ── VAPI Assistant prompt for recruitment screening ─────────────
RECRUITMENT_SYSTEM_PROMPT = """You are a friendly, professional recruitment assistant calling on behalf of a recruitment agency.

Your goal is to have a SHORT screening call (under 2 minutes).

CONVERSATION FLOW:
1. Greet the candidate by first name. Introduce yourself: "Hi {first_name}, this is an AI assistant calling from the recruitment team. I hope I'm not catching you at a bad time?"
2. If they say it's a bad time, politely ask when would be better, note it, and end the call.
3. Ask the KEY QUESTION: "I'm reaching out because we have your profile on file. I just wanted to check — are you currently open to new opportunities, or are you actively looking for a new role?"
4. Based on their answer:
   - If ACTIVELY LOOKING: Say "That's great to hear!" Then ask: "Could you briefly tell me what kind of role or location you're looking for?" Note their answer.
   - If OPEN BUT NOT ACTIVELY LOOKING: Say "Understood, good to know. We'll keep you in mind for anything relevant."
   - If NOT LOOKING: Say "No problem at all. Thanks for letting me know. We'll update our records."
   - If they say WRONG NUMBER or they're not who we're looking for: Apologise and end politely.
5. Thank them for their time and end the call.

RULES:
- Be concise and respectful of their time
- Do NOT pressure anyone
- If they ask to be removed from the list, confirm you'll do so immediately
- Keep the entire call under 2 minutes
- Speak naturally and conversationally
- If you detect voicemail, leave a brief message: "Hi {first_name}, this is a call from the recruitment team. We were checking if you're open to new opportunities. No need to call back — we may try again another time. Thanks!"
"""

# Campaign-specific screening prompt template
CAMPAIGN_SCREENING_PROMPT = """You are a friendly, professional recruitment assistant calling on behalf of a recruitment agency.

You are conducting a preliminary screening call for the following role:
JOB ROLE: {job_role}

JOB DESCRIPTION:
{job_description}

Your goal is to have a SHORT preliminary screening call (under 3 minutes). Keep it simple and conversational.

CONVERSATION FLOW:
1. Greet the candidate by first name: "Hi {{first_name}}, this is an AI assistant calling from the recruitment team. I hope I'm not catching you at a bad time?"
2. If they say it's a bad time, politely ask when would be better, note it, and end the call.
3. Briefly mention the role: "We're currently looking for a {job_role} and your profile caught our attention. Are you open to hearing more?"
4. If they're interested, ask these simple screening questions one at a time (keep each question short):
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
- Do NOT ask overly technical or complex questions — this is a preliminary screening
- If you detect voicemail, leave a brief message: "Hi {{first_name}}, this is a call from the recruitment team regarding a {job_role} position. No need to call back — we may try again another time. Thanks!"
"""

# Analysis prompt — VAPI will run this after the call to extract structured data
ANALYSIS_PROMPT = """Analyze the call transcript carefully and extract the following:

1. disposition: Choose EXACTLY ONE based on what the candidate ACTUALLY SAID:
   - QUALIFIED: Candidate is interested/looking for a job or open to opportunities. If a person says they are looking for a job, mark them as QUALIFIED.
   - PARTIALLY_QUALIFIED: Candidate is interested but may lack some required experience
   - NOT_QUALIFIED: Candidate lacks required experience or is clearly not a fit
   - NOT_LOOKING: Candidate said they are NOT looking, NOT interested, NOT open to new roles, happy where they are, or declined the opportunity
   - CALL_BACK: Candidate said it's a bad time, asked to be called back later, or said they're busy right now
   - WRONG_NUMBER: Wrong person or wrong number
   - DNC: Candidate explicitly asked to be removed from the call list or said do not call again

IMPORTANT: Pay close attention to negation. If the candidate says "I am NOT looking" or "not interested" or "not open", the disposition MUST be NOT_LOOKING.
If the candidate says they ARE looking or ARE open to opportunities, the disposition MUST be QUALIFIED.

2. summary: A detailed 2-3 sentence summary of the call outcome. Include whether the candidate is interested, their relevant experience/skills, preferred location, and availability.

3. location: Any location/area the candidate mentioned (empty string if none).

4. availability: Any availability or timeline mentioned (empty string if none).

Return as JSON:
{
  "disposition": "...",
  "summary": "...",
  "location": "...",
  "availability": "..."
}
"""


class VAPIClient:
    """Async client for the VAPI voice AI platform."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = settings.vapi_base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {settings.vapi_api_key}",
            "Content-Type": "application/json",
        }
        self._http: Optional[httpx.AsyncClient] = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                headers=self.headers,
                timeout=30.0,
            )
        return self._http

    async def close(self) -> None:
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    # ── Assistant management ────────────────────────────────────

    async def create_assistant(self) -> str:
        """Create a VAPI assistant for recruitment screening. Returns assistant ID."""
        client = await self._client()

        payload = {
            "name": "Recruitment Screener",
            "model": {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "messages": [
                    {
                        "role": "system",
                        "content": RECRUITMENT_SYSTEM_PROMPT.replace(
                            "{job_role_section}", ""
                        ).replace("{job_role}", "open").replace("{first_name}", "there"),
                    }
                ],
                "temperature": 0.7,
            },
            "voice": {
                "provider": "11labs",
                "voiceId": "21m00Tcm4TlvDq8ikWAM",  # Rachel — natural female voice
            },
            "firstMessage": "Hello! Is this a good time to talk briefly?",
            "endCallMessage": "Thanks for your time. Have a great day!",
            "maxDurationSeconds": 180,  # 3 min hard cap
            "silenceTimeoutSeconds": 15,
            "analysisPlan": {
                "summaryPlan": {
                    "enabled": True,
                },
                "structuredDataPlan": {
                    "enabled": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "disposition": {
                                "type": "string",
                                "enum": [
                                    "QUALIFIED",
                                    "PARTIALLY_QUALIFIED",
                                    "NOT_QUALIFIED",
                                    "NOT_LOOKING",
                                    "CALL_BACK",
                                    "WRONG_NUMBER",
                                    "DNC",
                                ],
                                "description": "The call disposition — QUALIFIED if candidate is looking for a job",
                            },
                            "summary": {
                                "type": "string",
                                "description": "Detailed 2-3 sentence summary including skills, experience, and qualification assessment",
                            },
                            "location": {
                                "type": "string",
                                "description": "Location/area mentioned by candidate",
                            },
                            "availability": {
                                "type": "string",
                                "description": "Availability or timeline mentioned",
                            },
                        },
                    },
                    "messages": [
                        {
                            "role": "system",
                            "content": ANALYSIS_PROMPT,
                        }
                    ],
                },
            },
            "serverUrl": f"{self.settings.webhook_base_url}/webhook/vapi",
        }

        resp = await client.post("/assistant", json=payload)
        resp.raise_for_status()
        data = resp.json()
        assistant_id = data["id"]
        log.info("vapi_assistant_created", assistant_id=assistant_id)
        return assistant_id

    async def get_or_create_assistant(self) -> str:
        """Return existing assistant ID or create a new one."""
        if self.settings.vapi_assistant_id:
            log.info("using_existing_assistant", assistant_id=self.settings.vapi_assistant_id)
            return self.settings.vapi_assistant_id
        return await self.create_assistant()

    async def create_campaign_assistant(
        self,
        campaign_name: str,
        job_role: str,
        job_description: str = "",
        custom_prompt: str = "",
    ) -> str:
        """
        Create a VAPI assistant tailored to a specific campaign's job description.
        Generates simple preliminary screening questions based on the role.
        Returns the VAPI assistant ID.
        """
        client = await self._client()

        # Build screening questions from the job description
        screening_questions = self._generate_screening_questions(job_role, job_description, custom_prompt)

        # Build the campaign-specific prompt
        system_prompt = CAMPAIGN_SCREENING_PROMPT.format(
            job_role=job_role,
            job_description=job_description or f"We are hiring for a {job_role} position.",
            screening_questions=screening_questions,
        )

        # If user provided a custom prompt, append it
        if custom_prompt:
            system_prompt += f"\n\nADDITIONAL INSTRUCTIONS FROM THE RECRUITER:\n{custom_prompt}"

        payload = {
            "name": f"Screener — {campaign_name[:50]}",
            "model": {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "messages": [
                    {
                        "role": "system",
                        "content": system_prompt,
                    }
                ],
                "temperature": 0.7,
            },
            "voice": {
                "provider": "11labs",
                "voiceId": "21m00Tcm4TlvDq8ikWAM",
            },
            "firstMessage": "Hello! Is this a good time to talk briefly?",
            "endCallMessage": "Thanks for your time. Have a great day!",
            "maxDurationSeconds": 240,  # 4 min cap for screening questions
            "silenceTimeoutSeconds": 15,
            "analysisPlan": {
                "summaryPlan": {
                    "enabled": True,
                },
                "structuredDataPlan": {
                    "enabled": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "disposition": {
                                "type": "string",
                                "enum": [
                                    "QUALIFIED",
                                    "PARTIALLY_QUALIFIED",
                                    "NOT_QUALIFIED",
                                    "NOT_LOOKING",
                                    "CALL_BACK",
                                    "WRONG_NUMBER",
                                    "DNC",
                                ],
                                "description": "The call disposition — QUALIFIED if candidate is looking for a job",
                            },
                            "summary": {
                                "type": "string",
                                "description": "Detailed 2-3 sentence summary including skills, experience, and qualification assessment",
                            },
                            "location": {
                                "type": "string",
                                "description": "Location/area mentioned by candidate",
                            },
                            "availability": {
                                "type": "string",
                                "description": "Availability or timeline mentioned",
                            },
                        },
                    },
                    "messages": [
                        {
                            "role": "system",
                            "content": ANALYSIS_PROMPT,
                        }
                    ],
                },
            },
            "serverUrl": f"{self.settings.webhook_base_url}/webhook/vapi",
        }

        resp = await client.post("/assistant", json=payload)
        resp.raise_for_status()
        data = resp.json()
        assistant_id = data["id"]
        log.info("campaign_assistant_created", assistant_id=assistant_id, campaign=campaign_name)
        return assistant_id

    @staticmethod
    def _generate_screening_questions(
        job_role: str,
        job_description: str = "",
        custom_prompt: str = "",
    ) -> str:
        """
        Generate skill-based screening questions from the job role and description.
        Extracts key skills/requirements and creates conversational questions
        about the candidate's experience with each.
        """
        from app.media_stream import generate_screening_questions
        return generate_screening_questions(job_role, job_description)

    # ── Outbound calls ──────────────────────────────────────────

    async def place_call(
        self,
        phone_e164: str,
        assistant_id: str,
        candidate_name: str = "",
        record_id: str = "",
        job_role: str = "",
        phone_number_id: str = "",
    ) -> dict:
        """
        Place an outbound call via VAPI.

        The assistant_id should be a campaign-specific assistant that already
        has the job-description-aware prompt baked in. We only override the
        firstMessage to personalise with the candidate's name.

        Args:
            phone_number_id: VAPI phone number ID to call from.
                             Falls back to settings.vapi_phone_number_id if empty.

        Returns the VAPI call object (contains 'id', 'status', etc.).
        """
        client = await self._client()

        name = candidate_name or "there"

        # Only override the greeting with the candidate's name —
        # the campaign assistant already has the right screening prompt
        assistant_overrides = {
            "firstMessage": f"Hi {name}! Is this a good time to talk briefly?",
        }

        # Use provided phone_number_id or fall back to global setting
        caller_phone_id = phone_number_id or self.settings.vapi_phone_number_id

        payload = {
            "assistantId": assistant_id,
            "phoneNumberId": caller_phone_id,
            "customer": {
                "number": phone_e164,
            },
            "metadata": {
                "unique_record_id": record_id,
                "job_role": job_role,
            },
            "assistantOverrides": assistant_overrides,
        }

        log.info(
            "placing_call",
            phone=phone_e164,
            record_id=record_id,
            candidate=candidate_name,
            job_role=job_role,
        )

        resp = await client.post("/call/phone", json=payload)
        resp.raise_for_status()
        data = resp.json()
        log.info("call_placed", vapi_call_id=data.get("id"), status=data.get("status"))
        return data

    async def get_call(self, call_id: str) -> dict:
        """Fetch call details from VAPI."""
        client = await self._client()
        resp = await client.get(f"/call/{call_id}")
        resp.raise_for_status()
        return resp.json()
