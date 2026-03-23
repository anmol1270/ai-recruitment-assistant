"""
Twilio phone number management and outbound calling service.

Handles:
  - Searching available numbers via Twilio
  - Purchasing and releasing numbers
  - Placing outbound AI calls (Twilio + OpenAI Realtime via Media Streams)
  - TwiML generation for AI voice agent conversations
"""

from __future__ import annotations

import json
import structlog
from typing import Optional
from urllib.parse import urlencode

from twilio.rest import Client as TwilioClient
from twilio.base.exceptions import TwilioRestException

from app.config import Settings

log = structlog.get_logger(__name__)

# Twilio approximate pricing by country (fallback)
TWILIO_PRICING_FALLBACK = {
    "US": 1.00,
    "GB": 1.00,
    "CA": 1.00,
    "AU": 3.50,
    "FR": 1.50,
    "DE": 1.50,
    "AE": 6.00,
    "IN": 2.00,
    "IE": 1.50,
}


class TwilioService:
    """Async-compatible Twilio voice and phone number management."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.account_sid = settings.twilio_account_sid
        self.auth_token = settings.twilio_auth_token
        self.markup = settings.phone_number_markup
        self._client: Optional[TwilioClient] = None

    @property
    def client(self) -> TwilioClient:
        if self._client is None:
            self._client = TwilioClient(self.account_sid, self.auth_token)
        return self._client

    async def close(self) -> None:
        """Cleanup (Twilio SDK manages its own connections)."""
        self._client = None

    # ── Search available numbers ────────────────────────────────

    async def search_available_numbers(
        self,
        country_code: str = "US",
        area_code: str = "",
        contains: str = "",
        number_type: str = "Local",
        limit: int = 20,
    ) -> list[dict]:
        """Search Twilio for available phone numbers."""
        try:
            kwargs: dict = {"limit": min(limit, 30)}
            if area_code:
                kwargs["area_code"] = area_code
            if contains:
                kwargs["contains"] = contains

            if number_type.lower() == "tollfree":
                numbers = self.client.available_phone_numbers(country_code).toll_free.list(**kwargs)
            else:
                numbers = self.client.available_phone_numbers(country_code).local.list(**kwargs)

        except TwilioRestException as e:
            log.error("twilio_search_failed", error=str(e))
            raise ValueError(f"Twilio search failed: {e.msg}")
        except Exception as e:
            log.error("twilio_search_error", error=str(e))
            raise ValueError(f"Failed to search numbers: {e}")

        if not numbers:
            raise ValueError(f"No available numbers for country {country_code}")

        base_price = self._get_country_price(country_code, number_type)

        results = []
        for n in numbers:
            capabilities = n.capabilities or {}
            results.append({
                "phone_number": n.phone_number,
                "friendly_name": n.friendly_name or n.phone_number,
                "country_code": country_code,
                "region": getattr(n, "region", ""),
                "locality": getattr(n, "locality", ""),
                "capabilities": {
                    "voice": capabilities.get("voice", False),
                    "sms": capabilities.get("sms", False),
                    "mms": capabilities.get("mms", False),
                },
                "number_type": number_type,
                "twilio_price": base_price,
                "our_price": round(base_price + self.markup, 2),
                "markup": self.markup,
            })

        return results

    def _get_country_price(self, country_code: str, number_type: str = "Local") -> float:
        return TWILIO_PRICING_FALLBACK.get(country_code, 1.50)

    # ── Purchase a number ───────────────────────────────────────

    async def purchase_number(self, phone_number: str) -> dict:
        """Purchase a phone number from Twilio."""
        try:
            number = self.client.incoming_phone_numbers.create(
                phone_number=phone_number,
                voice_url=f"{self.settings.webhook_base_url}/webhook/twilio/voice",
                voice_method="POST",
                status_callback=f"{self.settings.webhook_base_url}/webhook/twilio/status",
                status_callback_method="POST",
            )
            log.info("twilio_number_purchased", phone=phone_number, sid=number.sid)

            capabilities = number.capabilities or {}
            return {
                "id": number.sid,
                "phone_number": number.phone_number,
                "sid": number.sid,
                "friendly_name": number.friendly_name or phone_number,
                "capabilities": {
                    "voice": capabilities.get("voice", False),
                    "sms": capabilities.get("sms", False),
                    "mms": capabilities.get("mms", False),
                },
            }
        except TwilioRestException as e:
            log.error("twilio_purchase_failed", error=str(e))
            if "already" in str(e).lower():
                raise ValueError("This number is already owned by your account")
            raise ValueError(f"Failed to purchase number: {e.msg}")
        except Exception as e:
            log.error("twilio_purchase_error", error=str(e))
            raise ValueError(f"Purchase failed: {e}")

    # ── Release a number ────────────────────────────────────────

    async def release_number(self, twilio_sid: str) -> bool:
        """Release (delete) a phone number from Twilio."""
        try:
            self.client.incoming_phone_numbers(twilio_sid).delete()
            log.info("twilio_number_released", sid=twilio_sid)
            return True
        except Exception as e:
            log.error("twilio_release_failed", sid=twilio_sid, error=str(e))
            return False

    # ── Outbound calling ────────────────────────────────────────

    async def place_call(
        self,
        phone_e164: str,
        from_number: str,
        candidate_name: str = "",
        record_id: str = "",
        job_role: str = "",
        assistant_config: Optional[dict] = None,
    ) -> dict:
        """
        Place an outbound call via Twilio.
        The call connects to our webhook which serves TwiML to start
        an OpenAI Realtime media stream for the AI conversation.
        """
        name = candidate_name or "there"

        # Pass metadata via status callback params
        status_callback_url = (
            f"{self.settings.webhook_base_url}/webhook/twilio/status"
            f"?{urlencode({'record_id': record_id})}"
        )

        # Build the voice webhook URL with query params for context
        voice_url = (
            f"{self.settings.webhook_base_url}/webhook/twilio/voice"
            f"?{urlencode({'candidate_name': name, 'record_id': record_id, 'job_role': job_role})}"
        )

        try:
            call = self.client.calls.create(
                to=phone_e164,
                from_=from_number,
                url=voice_url,
                method="POST",
                status_callback=status_callback_url,
                status_callback_event=["initiated", "ringing", "answered", "completed"],
                status_callback_method="POST",
                machine_detection="DetectMessageEnd",
                machine_detection_timeout=5,
                timeout=30,
                record=True,
            )

            log.info(
                "call_placed",
                call_sid=call.sid,
                phone=phone_e164,
                record_id=record_id,
                candidate=candidate_name,
            )

            return {
                "id": call.sid,
                "status": call.status,
                "phone": phone_e164,
                "from": from_number,
            }

        except TwilioRestException as e:
            log.error("twilio_call_failed", error=str(e), phone=phone_e164)
            raise
        except Exception as e:
            log.error("twilio_call_error", error=str(e), phone=phone_e164)
            raise

    async def get_call(self, call_sid: str) -> dict:
        """Fetch call details from Twilio."""
        call = self.client.calls(call_sid).fetch()
        return {
            "id": call.sid,
            "status": call.status,
            "duration": call.duration,
            "start_time": str(call.start_time) if call.start_time else "",
            "end_time": str(call.end_time) if call.end_time else "",
            "price": call.price,
            "direction": call.direction,
        }

    async def get_recording_url(self, call_sid: str) -> str:
        """Get the recording URL for a completed call."""
        recordings = self.client.calls(call_sid).recordings.list(limit=1)
        if recordings:
            return f"https://api.twilio.com{recordings[0].uri.replace('.json', '.mp3')}"
        return ""
