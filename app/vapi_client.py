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

# Analysis prompt — VAPI will run this after the call to extract structured data
ANALYSIS_PROMPT = """Analyze the call transcript and extract the following:

1. disposition: One of ACTIVE_LOOKING, NOT_LOOKING, CALL_BACK, WRONG_NUMBER, DNC
   - ACTIVE_LOOKING: Candidate is actively looking or open to opportunities
   - NOT_LOOKING: Candidate is not interested in new roles
   - CALL_BACK: Candidate asked to be called back later
   - WRONG_NUMBER: Wrong person or number
   - DNC: Candidate asked to be removed from the list

2. summary: A 1-2 sentence summary of the call outcome.

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
                        "content": RECRUITMENT_SYSTEM_PROMPT,
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
                                    "ACTIVE_LOOKING",
                                    "NOT_LOOKING",
                                    "CALL_BACK",
                                    "WRONG_NUMBER",
                                    "DNC",
                                ],
                                "description": "The call disposition based on candidate response",
                            },
                            "summary": {
                                "type": "string",
                                "description": "1-2 sentence summary of the call",
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

    # ── Outbound calls ──────────────────────────────────────────

    async def place_call(
        self,
        phone_e164: str,
        assistant_id: str,
        candidate_name: str = "",
        record_id: str = "",
    ) -> dict:
        """
        Place an outbound call via VAPI.

        Returns the VAPI call object (contains 'id', 'status', etc.).
        """
        client = await self._client()

        # Inject candidate name into the system prompt for personalisation
        assistant_overrides = {}
        if candidate_name:
            personalised_prompt = RECRUITMENT_SYSTEM_PROMPT.replace(
                "{first_name}", candidate_name
            )
            assistant_overrides = {
                "model": {
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "messages": [
                        {
                            "role": "system",
                            "content": personalised_prompt,
                        }
                    ],
                },
                "firstMessage": f"Hi {candidate_name}! Is this a good time to talk briefly?",
            }

        payload = {
            "assistantId": assistant_id,
            "phoneNumberId": self.settings.vapi_phone_number_id,
            "customer": {
                "number": phone_e164,
            },
            "metadata": {
                "unique_record_id": record_id,
            },
        }

        if assistant_overrides:
            payload["assistantOverrides"] = assistant_overrides

        log.info(
            "placing_call",
            phone=phone_e164,
            record_id=record_id,
            candidate=candidate_name,
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
