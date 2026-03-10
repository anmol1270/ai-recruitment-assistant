"""
Twilio phone number management service.

Handles searching available numbers, purchasing, and releasing via the Twilio REST API.
Also registers purchased numbers with VAPI for outbound calling.
"""

from __future__ import annotations

import httpx
import structlog
from typing import Optional

log = structlog.get_logger(__name__)

# Twilio base monthly pricing by country (fallback if API doesn't return price)
# These are approximate — real prices come from Twilio's API
TWILIO_PRICING_FALLBACK = {
    "US": 1.00,
    "GB": 1.00,
    "CA": 1.00,
    "AU": 2.75,
    "FR": 1.15,
    "DE": 1.15,
    "AE": 6.00,
    "IN": 2.00,
    "IE": 1.15,
}


class TwilioService:
    """Async Twilio phone number management."""

    TWILIO_API = "https://api.twilio.com/2010-04-01"

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        markup: float = 0.50,
        vapi_api_key: str = "",
    ):
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.markup = markup
        self.vapi_api_key = vapi_api_key
        self._http: Optional[httpx.AsyncClient] = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                auth=(self.account_sid, self.auth_token),
                timeout=30.0,
            )
        return self._http

    async def close(self) -> None:
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    # ── Search available numbers ────────────────────────────────

    async def search_available_numbers(
        self,
        country_code: str = "US",
        area_code: str = "",
        contains: str = "",
        number_type: str = "Local",
        limit: int = 20,
    ) -> list[dict]:
        """
        Search Twilio for available phone numbers.
        Returns list of numbers with pricing (including our markup).
        """
        client = await self._client()
        base = f"{self.TWILIO_API}/Accounts/{self.account_sid}"

        # Build query params
        params = {"PageSize": min(limit, 30)}
        if area_code:
            params["AreaCode"] = area_code
        if contains:
            params["Contains"] = contains

        # Try Local, then TollFree, then Mobile
        url = f"{base}/AvailablePhoneNumbers/{country_code}/{number_type}.json"

        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            log.error("twilio_search_failed", status=e.response.status_code, detail=e.response.text)
            raise ValueError(f"Twilio search failed: {e.response.status_code}")
        except Exception as e:
            log.error("twilio_search_error", error=str(e))
            raise ValueError(f"Failed to search numbers: {e}")

        numbers = data.get("available_phone_numbers", [])

        # Get base price for this country
        twilio_price = await self._get_country_price(country_code, number_type)

        results = []
        for n in numbers:
            results.append({
                "phone_number": n["phone_number"],
                "friendly_name": n.get("friendly_name", n["phone_number"]),
                "country_code": country_code,
                "region": n.get("region", ""),
                "locality": n.get("locality", ""),
                "capabilities": {
                    "voice": n.get("capabilities", {}).get("voice", False),
                    "sms": n.get("capabilities", {}).get("SMS", False),
                    "mms": n.get("capabilities", {}).get("MMS", False),
                },
                "number_type": number_type,
                "twilio_price": twilio_price,
                "our_price": round(twilio_price + self.markup, 2),
                "markup": self.markup,
            })

        return results

    async def _get_country_price(
        self, country_code: str, number_type: str = "Local"
    ) -> float:
        """Fetch Twilio's phone number pricing for a country."""
        client = await self._client()
        url = f"{self.TWILIO_API}/Accounts/{self.account_sid}/IncomingPhoneNumbers/Local/Pricing.json"

        # Try the pricing API
        try:
            pricing_url = f"https://pricing.twilio.com/v2/PhoneNumbers/Countries/{country_code}"
            resp = await client.get(pricing_url)
            if resp.status_code == 200:
                data = resp.json()
                # Find matching price
                for price_info in data.get("phone_number_prices", []):
                    if price_info.get("number_type", "").lower() == number_type.lower():
                        base_price = float(price_info.get("base_price", 0))
                        current_price = float(price_info.get("current_price", base_price))
                        if current_price > 0:
                            return current_price
        except Exception:
            pass  # Fall back to defaults

        return TWILIO_PRICING_FALLBACK.get(country_code, 1.50)

    # ── Purchase a number ───────────────────────────────────────

    async def purchase_number(self, phone_number: str) -> dict:
        """
        Purchase a phone number from Twilio.
        Returns the Twilio IncomingPhoneNumber resource.
        """
        client = await self._client()
        url = f"{self.TWILIO_API}/Accounts/{self.account_sid}/IncomingPhoneNumbers.json"

        payload = {
            "PhoneNumber": phone_number,
            "VoiceUrl": "",  # Will be handled by VAPI
            "FriendlyName": f"RecruitAI - {phone_number}",
        }

        try:
            resp = await client.post(url, data=payload)
            resp.raise_for_status()
            data = resp.json()
            log.info("twilio_number_purchased", phone=phone_number, sid=data.get("sid"))
            return data
        except httpx.HTTPStatusError as e:
            detail = e.response.text
            log.error("twilio_purchase_failed", status=e.response.status_code, detail=detail)
            if "already own" in detail.lower() or "21422" in detail:
                raise ValueError("This number is already owned by your account")
            raise ValueError(f"Failed to purchase number: {e.response.status_code}")
        except Exception as e:
            log.error("twilio_purchase_error", error=str(e))
            raise ValueError(f"Purchase failed: {e}")

    # ── Release a number ────────────────────────────────────────

    async def release_number(self, twilio_sid: str) -> bool:
        """Release (delete) a phone number from Twilio."""
        client = await self._client()
        url = f"{self.TWILIO_API}/Accounts/{self.account_sid}/IncomingPhoneNumbers/{twilio_sid}.json"

        try:
            resp = await client.delete(url)
            resp.raise_for_status()
            log.info("twilio_number_released", sid=twilio_sid)
            return True
        except Exception as e:
            log.error("twilio_release_failed", sid=twilio_sid, error=str(e))
            return False

    # ── Register with VAPI ──────────────────────────────────────

    async def register_with_vapi(
        self,
        phone_number: str,
        twilio_sid: str,
    ) -> str:
        """
        Import a Twilio number into VAPI so it can be used for outbound calls.
        Returns the VAPI phone number ID.
        """
        if not self.vapi_api_key:
            raise ValueError("VAPI API key not configured")

        client = await self._client()

        payload = {
            "provider": "twilio",
            "number": phone_number,
            "twilioAccountSid": self.account_sid,
            "twilioAuthToken": self.auth_token,
            "name": f"RecruitAI - {phone_number}",
        }

        try:
            resp = await client.post(
                "https://api.vapi.ai/phone-number",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.vapi_api_key}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            vapi_id = data.get("id", "")
            log.info("vapi_number_registered", phone=phone_number, vapi_id=vapi_id)
            return vapi_id
        except httpx.HTTPStatusError as e:
            log.error("vapi_register_failed", status=e.response.status_code, detail=e.response.text)
            # Number might already be registered — try to find it
            if e.response.status_code in (400, 409):
                existing_id = await self._find_vapi_number(phone_number)
                if existing_id:
                    return existing_id
            raise ValueError(f"Failed to register with VAPI: {e.response.status_code}")
        except Exception as e:
            log.error("vapi_register_error", error=str(e))
            raise ValueError(f"VAPI registration failed: {e}")

    async def _find_vapi_number(self, phone_number: str) -> str:
        """Find a phone number already registered in VAPI."""
        client = await self._client()
        try:
            resp = await client.get(
                "https://api.vapi.ai/phone-number",
                headers={
                    "Authorization": f"Bearer {self.vapi_api_key}",
                },
            )
            if resp.status_code == 200:
                for num in resp.json():
                    if num.get("number") == phone_number:
                        return num.get("id", "")
        except Exception:
            pass
        return ""

    async def delete_from_vapi(self, vapi_phone_id: str) -> bool:
        """Delete a phone number from VAPI."""
        if not self.vapi_api_key or not vapi_phone_id:
            return False

        client = await self._client()
        try:
            resp = await client.delete(
                f"https://api.vapi.ai/phone-number/{vapi_phone_id}",
                headers={
                    "Authorization": f"Bearer {self.vapi_api_key}",
                },
            )
            return resp.status_code in (200, 204)
        except Exception as e:
            log.error("vapi_delete_failed", vapi_id=vapi_phone_id, error=str(e))
            return False
