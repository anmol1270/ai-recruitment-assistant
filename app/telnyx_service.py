"""
Telnyx phone number management service.

Handles searching available numbers, purchasing, and releasing via the Telnyx v2 API.
Also registers purchased numbers with VAPI for outbound calling.
"""

from __future__ import annotations

import httpx
import structlog
from typing import Optional

log = structlog.get_logger(__name__)

# Telnyx base monthly pricing by country (fallback if API doesn't return price)
# These are approximate — real prices come from Telnyx's API
TELNYX_PRICING_FALLBACK = {
    "US": 1.00,
    "GB": 1.00,
    "CA": 1.00,
    "AU": 3.00,
    "FR": 1.50,
    "DE": 1.50,
    "AE": 6.00,
    "IN": 2.00,
    "IE": 1.50,
}


class TelnyxService:
    """Async Telnyx phone number management."""

    TELNYX_API = "https://api.telnyx.com/v2"

    def __init__(
        self,
        api_key: str,
        markup: float = 0.50,
        vapi_api_key: str = "",
    ):
        self.api_key = api_key
        self.markup = markup
        self.vapi_api_key = vapi_api_key
        self._http: Optional[httpx.AsyncClient] = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
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
        Search Telnyx for available phone numbers.
        Returns list of numbers with pricing (including our markup).
        """
        client = await self._client()
        url = f"{self.TELNYX_API}/available_phone_numbers"

        # Build query params
        params: dict = {
            "filter[country_code]": country_code,
            "filter[limit]": min(limit, 30),
        }

        # Map number_type to Telnyx features filter
        if number_type.lower() == "tollfree":
            params["filter[phone_number_type]"] = "toll_free"
        else:
            params["filter[phone_number_type]"] = "local"

        if area_code:
            params["filter[national_destination_code]"] = area_code
        if contains:
            params["filter[phone_number][contains]"] = contains

        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            log.error("telnyx_search_failed", status=e.response.status_code, detail=e.response.text)
            raise ValueError(f"Telnyx search failed: {e.response.status_code}")
        except Exception as e:
            log.error("telnyx_search_error", error=str(e))
            raise ValueError(f"Failed to search numbers: {e}")

        numbers = data.get("data", [])

        if not numbers:
            raise ValueError(
                f"No available numbers for country {country_code}"
            )

        # Get base price for this country
        telnyx_price = self._get_country_price(country_code, number_type)

        results = []
        for n in numbers:
            features = n.get("features", [])
            phone_number = n.get("phone_number", "")
            cost = float(n.get("cost_information", {}).get("monthly_cost", 0) or telnyx_price)

            results.append({
                "phone_number": phone_number,
                "friendly_name": phone_number,
                "country_code": country_code,
                "region": n.get("region_information", [{}])[0].get("region_name", "") if n.get("region_information") else "",
                "locality": "",
                "capabilities": {
                    "voice": "voice" in [f.get("name", "") for f in features],
                    "sms": "sms" in [f.get("name", "") for f in features],
                    "mms": "mms" in [f.get("name", "") for f in features],
                },
                "number_type": number_type,
                "telnyx_price": cost,
                "our_price": round(cost + self.markup, 2),
                "markup": self.markup,
            })

        return results

    def _get_country_price(
        self, country_code: str, number_type: str = "Local"
    ) -> float:
        """Get fallback Telnyx phone number pricing for a country."""
        return TELNYX_PRICING_FALLBACK.get(country_code, 1.50)

    # ── Purchase a number ───────────────────────────────────────

    async def purchase_number(self, phone_number: str) -> dict:
        """
        Purchase a phone number from Telnyx (create a number order).
        Re-searches the exact number first so Telnyx's internal cache recognizes it.
        Returns the Telnyx number order resource.
        """
        client = await self._client()

        # Telnyx requires the number to appear in a recent search result.
        # Re-search for this exact number so it's fresh in their cache.
        search_url = f"{self.TELNYX_API}/available_phone_numbers"
        try:
            search_resp = await client.get(search_url, params={
                "filter[phone_number][starts_with]": phone_number,
                "filter[limit]": 1,
            })
            search_resp.raise_for_status()
            search_data = search_resp.json().get("data", [])
            if not search_data:
                raise ValueError(f"Number {phone_number} is no longer available on Telnyx")
            log.info("telnyx_pre_purchase_search_ok", phone=phone_number)
        except httpx.HTTPStatusError as e:
            log.warning("telnyx_pre_purchase_search_failed", status=e.response.status_code)
            # Still attempt the order
        except ValueError:
            raise

        url = f"{self.TELNYX_API}/number_orders"
        payload = {
            "phone_numbers": [{"phone_number": phone_number}],
            "customer_reference": "RecruitAI",
        }

        try:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json().get("data", {})
            log.info("telnyx_number_purchased", phone=phone_number, id=data.get("id"))

            # Retrieve the phone number details to get the resource ID
            phone_data = await self._get_phone_number_details(phone_number)

            return {
                "id": data.get("id", ""),
                "phone_number": phone_number,
                "sid": phone_data.get("id", data.get("id", "")),
                "friendly_name": phone_number,
                "capabilities": phone_data.get("features", []),
            }
        except httpx.HTTPStatusError as e:
            detail = e.response.text
            log.error("telnyx_purchase_failed", status=e.response.status_code, detail=detail)
            if "already" in detail.lower():
                raise ValueError("This number is already owned by your account")
            raise ValueError(f"Failed to purchase number: {e.response.status_code}")
        except ValueError:
            raise
        except Exception as e:
            log.error("telnyx_purchase_error", error=str(e))
            raise ValueError(f"Purchase failed: {e}")

    async def _get_phone_number_details(self, phone_number: str) -> dict:
        """Fetch details of an owned phone number from Telnyx."""
        client = await self._client()
        url = f"{self.TELNYX_API}/phone_numbers"
        params = {"filter[phone_number]": phone_number}
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json().get("data", [])
            if data:
                return data[0]
        except Exception:
            pass
        return {}

    # ── Release a number ────────────────────────────────────────

    async def release_number(self, telnyx_id: str) -> bool:
        """Release (delete) a phone number from Telnyx."""
        client = await self._client()
        url = f"{self.TELNYX_API}/phone_numbers/{telnyx_id}"

        try:
            resp = await client.delete(url)
            resp.raise_for_status()
            log.info("telnyx_number_released", id=telnyx_id)
            return True
        except Exception as e:
            log.error("telnyx_release_failed", id=telnyx_id, error=str(e))
            return False

    # ── Register with VAPI ──────────────────────────────────────

    async def register_with_vapi(
        self,
        phone_number: str,
        telnyx_id: str,
    ) -> str:
        """
        Import a Telnyx number into VAPI so it can be used for outbound calls.
        Returns the VAPI phone number ID.
        """
        if not self.vapi_api_key:
            raise ValueError("VAPI API key not configured")

        client = await self._client()

        payload = {
            "provider": "telnyx",
            "number": phone_number,
            "telnyxApiKey": self.api_key,
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
