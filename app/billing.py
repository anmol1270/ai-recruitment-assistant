"""
Stripe billing integration — subscriptions, checkout, webhooks.

Plans:
  - free:  50 calls/month, $0
  - pro:   500 calls/month, $49/month
"""

from __future__ import annotations

import stripe
import structlog
from typing import Optional

log = structlog.get_logger(__name__)

PLANS = {
    "free": {"name": "Free", "price": 0, "calls": 50},
    "pro":  {"name": "Pro",  "price": 4900, "calls": 500},  # cents
}


class BillingManager:
    """Stripe billing integration."""

    def __init__(
        self,
        stripe_secret_key: str,
        stripe_webhook_secret: str,
        stripe_pro_price_id: str,
        base_url: str,
    ):
        self.stripe_secret_key = stripe_secret_key
        self.stripe_webhook_secret = stripe_webhook_secret
        self.stripe_pro_price_id = stripe_pro_price_id
        self.base_url = base_url.rstrip("/")
        stripe.api_key = stripe_secret_key

    async def get_or_create_customer(
        self, user_id: int, email: str, name: str = ""
    ) -> str:
        """Get or create a Stripe customer. Returns customer ID."""
        # Search for existing
        customers = stripe.Customer.search(
            query=f'email:"{email}"',
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
    ) -> str:
        """Create a Stripe Checkout session for Pro plan. Returns checkout URL."""
        session = stripe.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            line_items=[{
                "price": self.stripe_pro_price_id,
                "quantity": 1,
            }],
            success_url=f"{self.base_url}/?billing=success",
            cancel_url=f"{self.base_url}/?billing=cancelled",
            metadata={"user_id": str(user_id)},
        )
        return session.url

    async def create_portal_session(self, customer_id: str) -> str:
        """Create Stripe billing portal session. Returns portal URL."""
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{self.base_url}/",
        )
        return session.url

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
