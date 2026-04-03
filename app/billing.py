"""
Stripe billing integration — subscriptions, checkout, webhooks.

Plans:
  - free:       50 calls/month, $0
  - starter:    500 calls/month, $49/month
  - pro:        1500 calls/month, $99/month
  - enterprise: 5000 calls/month, $200/month
"""

from __future__ import annotations

import stripe
import structlog
from typing import Optional

log = structlog.get_logger(__name__)

PLANS = {
    "free":       {"name": "Free Trial", "price": 0,     "calls": 5,       "max_candidates": 10, "max_phones": 1},
    "starter":    {"name": "Starter",    "price": 4900,  "calls": 500,     "max_candidates": 0,  "max_phones": 1},
    "pro":        {"name": "Professional", "price": 9900,  "calls": 1500,  "max_candidates": 0,  "max_phones": 3},
    "enterprise": {"name": "Enterprise", "price": 20000, "calls": 5000,    "max_candidates": 0,  "max_phones": 10},
    "admin":      {"name": "Admin",      "price": 0,     "calls": 999999,  "max_candidates": 0,  "max_phones": 999},
}


class BillingManager:
    """Stripe billing integration."""

    def __init__(
        self,
        stripe_secret_key: str,
        stripe_webhook_secret: str,
        stripe_starter_price_id: str,
        stripe_pro_price_id: str,
        stripe_enterprise_price_id: str,
        base_url: str,
    ):
        self.stripe_secret_key = stripe_secret_key
        self.stripe_webhook_secret = stripe_webhook_secret
        self.price_ids = {
            "starter": stripe_starter_price_id,
            "pro": stripe_pro_price_id,
            "enterprise": stripe_enterprise_price_id,
        }
        # Reverse lookup: price_id -> plan name
        self.price_to_plan = {v: k for k, v in self.price_ids.items() if v}
        self.base_url = base_url.rstrip("/")
        stripe.api_key = stripe_secret_key

    async def get_or_create_customer(
        self, user_id: int, email: str, name: str = ""
    ) -> str:
        """Get or create a Stripe customer. Returns customer ID."""
        # Search for existing
        customers = stripe.Customer.list(
            email=email,
            limit=1,
        )
        if customers.data:
            return customers.data[0].id

        # Create new
        customer = stripe.Customer.create(
            email=email,
            name=name,
            metadata={"user_id": str(user_id)},
        )
        return customer.id

    async def create_checkout_session(
        self,
        customer_id: str,
        user_id: int,
        plan: str = "starter",
    ) -> str:
        """Create a Stripe Checkout session for a paid plan. Returns checkout URL."""
        price_id = self.price_ids.get(plan)
        if not price_id:
            raise ValueError(f"Invalid plan: {plan}")
        session = stripe.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            line_items=[{
                "price": price_id,
                "quantity": 1,
            }],
            success_url=f"{self.base_url}/?billing=success",
            cancel_url=f"{self.base_url}/?billing=cancelled",
            metadata={"user_id": str(user_id), "plan": plan},
        )
        return session.url

    async def create_portal_session(self, customer_id: str) -> str:
        """Create Stripe billing portal session. Returns portal URL."""
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{self.base_url}/",
        )
        return session.url

    def resolve_plan_from_price(self, price_id: str) -> str:
        """Resolve plan name from a Stripe price ID. Falls back to 'starter'."""
        return self.price_to_plan.get(price_id, "starter")

    def verify_webhook(self, payload: bytes, signature: str) -> Optional[dict]:
        """Verify and parse Stripe webhook event."""
        try:
            event = stripe.Webhook.construct_event(
                payload, signature, self.stripe_webhook_secret,
            )
            return event
        except (stripe.error.SignatureVerificationError, ValueError) as e:
            log.warning("stripe_webhook_verification_failed", error=str(e))
            return None

    def get_subscription(self, subscription_id: str) -> Optional[dict]:
        """Fetch subscription details."""
        try:
            return stripe.Subscription.retrieve(subscription_id)
        except stripe.error.StripeError:
            return None

    async def create_phone_checkout_session(
        self,
        customer_id: str,
        user_id: int,
        phone_number: str,
        country_code: str,
        price_cents: int,
        twilio_price: float,
    ) -> str:
        """
        Create a one-time Stripe Checkout session for purchasing a phone number.
        On success, the webhook will trigger the actual Twilio purchase.
        Returns the checkout URL.
        """
        session = stripe.checkout.Session.create(
            customer=customer_id,
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": f"Phone Number: {phone_number}",
                        "description": f"Twilio phone number for outbound calling ({country_code})",
                    },
                    "unit_amount": price_cents,
                },
                "quantity": 1,
            }],
            success_url=f"{self.base_url}/?phone_purchase=success",
            cancel_url=f"{self.base_url}/?phone_purchase=cancelled",
            metadata={
                "user_id": str(user_id),
                "type": "phone_purchase",
                "phone_number": phone_number,
                "country_code": country_code,
                "twilio_price": str(twilio_price),
                "our_price": str(price_cents / 100),
            },
        )
        return session.url
