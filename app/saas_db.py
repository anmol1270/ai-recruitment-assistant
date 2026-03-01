"""
PostgreSQL database layer for the multi-tenant SaaS platform.
Uses asyncpg for async database operations.
"""

from __future__ import annotations

import asyncpg
import structlog
from datetime import datetime, timezone
from typing import Optional

log = structlog.get_logger(__name__)

# ── Schema DDL ──────────────────────────────────────────────────
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    google_id       VARCHAR(255) UNIQUE,
    email           VARCHAR(255) UNIQUE NOT NULL,
    name            VARCHAR(255),
    avatar_url      TEXT,
    plan            VARCHAR(20) DEFAULT 'free',
    stripe_customer_id      VARCHAR(255),
    stripe_subscription_id  VARCHAR(255),
    calls_this_month        INT DEFAULT 0,
    monthly_call_limit      INT DEFAULT 50,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS campaigns (
    id              SERIAL PRIMARY KEY,
    user_id         INT REFERENCES users(id) ON DELETE CASCADE,
    name            VARCHAR(255) NOT NULL,
    job_role        VARCHAR(255) NOT NULL,
    description     TEXT DEFAULT '',
    status          VARCHAR(20) DEFAULT 'draft',
    custom_prompt   TEXT DEFAULT '',
    total_candidates   INT DEFAULT 0,
    total_called       INT DEFAULT 0,
    vapi_assistant_id  VARCHAR(255) DEFAULT '',
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS candidates (
    id                  SERIAL PRIMARY KEY,
    campaign_id         INT REFERENCES campaigns(id) ON DELETE CASCADE,
    user_id             INT REFERENCES users(id) ON DELETE CASCADE,
    unique_record_id    VARCHAR(255),
    first_name          VARCHAR(255) DEFAULT '',
    last_name           VARCHAR(255) DEFAULT '',
    phone_e164          VARCHAR(50) NOT NULL,
    email               VARCHAR(255) DEFAULT '',
    status              VARCHAR(30) DEFAULT 'PENDING',
    vapi_call_id        VARCHAR(255) DEFAULT '',
    short_summary       TEXT DEFAULT '',
    raw_call_outcome    VARCHAR(100) DEFAULT '',
    transcript          TEXT DEFAULT '',
    recording_url       TEXT DEFAULT '',
    extracted_location      VARCHAR(255) DEFAULT '',
    extracted_availability  VARCHAR(255) DEFAULT '',
    attempt_count       INT DEFAULT 0,
    last_called_at      TIMESTAMP,
    created_at          TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS call_logs (
    id              SERIAL PRIMARY KEY,
    user_id         INT REFERENCES users(id) ON DELETE CASCADE,
    campaign_id     INT REFERENCES campaigns(id) ON DELETE CASCADE,
    candidate_id    INT,
    vapi_call_id    VARCHAR(255),
    action          VARCHAR(50),
    status          VARCHAR(30),
    detail          TEXT,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS usage (
    id          SERIAL PRIMARY KEY,
    user_id     INT REFERENCES users(id) ON DELETE CASCADE,
    month       VARCHAR(7) NOT NULL,
    calls_made  INT DEFAULT 0,
    calls_limit INT DEFAULT 50,
    UNIQUE(user_id, month)
);

CREATE INDEX IF NOT EXISTS idx_candidates_campaign ON candidates(campaign_id);
CREATE INDEX IF NOT EXISTS idx_candidates_user ON candidates(user_id);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);
CREATE INDEX IF NOT EXISTS idx_candidates_vapi_call ON candidates(vapi_call_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_user ON campaigns(user_id);
CREATE INDEX IF NOT EXISTS idx_call_logs_campaign ON call_logs(campaign_id);
CREATE INDEX IF NOT EXISTS idx_usage_user_month ON usage(user_id, month);
"""


class SaaSDatabase:
    """Multi-tenant PostgreSQL database layer."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        """Create connection pool and initialise schema."""
        self._pool = await asyncpg.create_pool(
            self.database_url,
            min_size=2,
            max_size=10,
        )
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
        log.info("database_connected", url=self.database_url[:30] + "...")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            log.info("database_closed")

    # ── User operations ─────────────────────────────────────────

    async def get_user_by_google_id(self, google_id: str) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE google_id = $1", google_id
            )
            return dict(row) if row else None

    async def get_user_by_email(self, email: str) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE email = $1", email
            )
            return dict(row) if row else None

    async def get_user_by_id(self, user_id: int) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE id = $1", user_id
            )
            return dict(row) if row else None

    async def create_user(
        self,
        google_id: str,
        email: str,
        name: str = "",
        avatar_url: str = "",
    ) -> dict:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO users (google_id, email, name, avatar_url)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (google_id) DO UPDATE SET
                       name = EXCLUDED.name,
                       avatar_url = EXCLUDED.avatar_url,
                       updated_at = NOW()
                   RETURNING *""",
                google_id, email, name, avatar_url,
            )
            return dict(row)

    async def update_user_plan(
        self,
        user_id: int,
        plan: str,
        monthly_call_limit: int,
        stripe_customer_id: str = "",
        stripe_subscription_id: str = "",
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """UPDATE users SET plan = $1, monthly_call_limit = $2,
                   stripe_customer_id = $3, stripe_subscription_id = $4,
                   updated_at = NOW()
                   WHERE id = $5""",
                plan, monthly_call_limit, stripe_customer_id,
                stripe_subscription_id, user_id,
            )

    async def update_user_stripe(
        self, user_id: int, stripe_customer_id: str
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET stripe_customer_id = $1 WHERE id = $2",
                stripe_customer_id, user_id,
            )

    # ── Campaign operations ─────────────────────────────────────

    async def create_campaign(
        self,
        user_id: int,
        name: str,
        job_role: str,
        description: str = "",
        custom_prompt: str = "",
    ) -> dict:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO campaigns (user_id, name, job_role, description, custom_prompt)
                   VALUES ($1, $2, $3, $4, $5)
                   RETURNING *""",
                user_id, name, job_role, description, custom_prompt,
            )
            return dict(row)

    async def get_campaigns(self, user_id: int) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT c.*,
                       (SELECT COUNT(*) FROM candidates WHERE campaign_id = c.id) as candidate_count,
                       (SELECT COUNT(*) FROM candidates WHERE campaign_id = c.id AND status != 'PENDING') as called_count
                   FROM campaigns c
                   WHERE c.user_id = $1
                   ORDER BY c.created_at DESC""",
                user_id,
            )
            return [dict(r) for r in rows]

    async def get_campaign(self, campaign_id: int, user_id: int) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM campaigns WHERE id = $1 AND user_id = $2",
                campaign_id, user_id,
            )
            return dict(row) if row else None

    async def update_campaign_status(
        self, campaign_id: int, user_id: int, status: str
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE campaigns SET status = $1, updated_at = NOW() WHERE id = $2 AND user_id = $3",
                status, campaign_id, user_id,
            )

    async def update_campaign_assistant(
        self, campaign_id: int, assistant_id: str
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE campaigns SET vapi_assistant_id = $1 WHERE id = $2",
                assistant_id, campaign_id,
            )

    async def delete_campaign(self, campaign_id: int, user_id: int) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM campaigns WHERE id = $1 AND user_id = $2",
                campaign_id, user_id,
            )
            return result == "DELETE 1"

    # ── Candidate operations ────────────────────────────────────

    async def add_candidates(
        self, campaign_id: int, user_id: int, candidates: list[dict]
    ) -> int:
        """Bulk insert candidates. Returns count inserted."""
        async with self._pool.acquire() as conn:
            count = 0
            for c in candidates:
                await conn.execute(
                    """INSERT INTO candidates
                       (campaign_id, user_id, unique_record_id, first_name,
                        last_name, phone_e164, email)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)
                       ON CONFLICT DO NOTHING""",
                    campaign_id, user_id,
                    c.get("unique_record_id", ""),
                    c.get("first_name", ""),
                    c.get("last_name", ""),
                    c["phone_e164"],
                    c.get("email", ""),
                )
                count += 1
            # Update campaign candidate count
            await conn.execute(
                """UPDATE campaigns SET total_candidates = (
                       SELECT COUNT(*) FROM candidates WHERE campaign_id = $1
                   ) WHERE id = $1""",
                campaign_id,
            )
            return count

    async def get_candidates(
        self, campaign_id: int, user_id: int, limit: int = 500
    ) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM candidates
                   WHERE campaign_id = $1 AND user_id = $2
                   ORDER BY created_at DESC LIMIT $3""",
                campaign_id, user_id, limit,
            )
            return [dict(r) for r in rows]

    async def get_pending_candidates(
        self, campaign_id: int, limit: int = 200
    ) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM candidates
                   WHERE campaign_id = $1
                     AND status IN ('PENDING', 'NO_ANSWER', 'BUSY', 'FAILED')
                   ORDER BY created_at ASC LIMIT $2""",
                campaign_id, limit,
            )
            return [dict(r) for r in rows]

    async def get_candidate_by_call_id(self, vapi_call_id: str) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM candidates WHERE vapi_call_id = $1",
                vapi_call_id,
            )
            return dict(row) if row else None

    async def get_candidate_by_record_id(
        self, unique_record_id: str
    ) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM candidates WHERE unique_record_id = $1",
                unique_record_id,
            )
            return dict(row) if row else None

    async def mark_call_started(
        self, candidate_id: int, vapi_call_id: str
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """UPDATE candidates SET
                       vapi_call_id = $1,
                       attempt_count = attempt_count + 1,
                       last_called_at = $2
                   WHERE id = $3""",
                vapi_call_id,
                datetime.now(timezone.utc),
                candidate_id,
            )

    async def update_call_result(
        self,
        vapi_call_id: str,
        status: str,
        short_summary: str = "",
        raw_call_outcome: str = "",
        transcript: str = "",
        recording_url: str = "",
        extracted_location: str = "",
        extracted_availability: str = "",
    ) -> Optional[dict]:
        """Update candidate after call ends. Returns the candidate dict."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE candidates SET
                       status = $1, short_summary = $2, raw_call_outcome = $3,
                       transcript = $4, recording_url = $5,
                       extracted_location = $6, extracted_availability = $7
                   WHERE vapi_call_id = $8
                   RETURNING *""",
                status, short_summary, raw_call_outcome,
                transcript, recording_url,
                extracted_location, extracted_availability,
                vapi_call_id,
            )
            if row:
                # Update campaign called count
                await conn.execute(
                    """UPDATE campaigns SET total_called = (
                           SELECT COUNT(*) FROM candidates
                           WHERE campaign_id = $1 AND status != 'PENDING'
                       ) WHERE id = $1""",
                    row["campaign_id"],
                )
            return dict(row) if row else None

    # ── Usage / billing operations ──────────────────────────────

    async def get_usage(self, user_id: int, month: str) -> dict:
        """Get or create usage record for a month (format: '2026-03')."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM usage WHERE user_id = $1 AND month = $2",
                user_id, month,
            )
            if row:
                return dict(row)
            # Create default
            user = await self.get_user_by_id(user_id)
            limit = user["monthly_call_limit"] if user else 50
            row = await conn.fetchrow(
                """INSERT INTO usage (user_id, month, calls_made, calls_limit)
                   VALUES ($1, $2, 0, $3)
                   ON CONFLICT (user_id, month) DO NOTHING
                   RETURNING *""",
                user_id, month, limit,
            )
            if row:
                return dict(row)
            return {"user_id": user_id, "month": month, "calls_made": 0, "calls_limit": limit}

    async def increment_usage(self, user_id: int, month: str) -> dict:
        """Increment call count and return updated usage."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO usage (user_id, month, calls_made, calls_limit)
                   VALUES ($1, $2, 1, 50)
                   ON CONFLICT (user_id, month)
                   DO UPDATE SET calls_made = usage.calls_made + 1
                   RETURNING *""",
                user_id, month,
            )
            return dict(row) if row else {}

    async def can_place_call(self, user_id: int) -> bool:
        """Check if user has remaining calls this month."""
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        usage = await self.get_usage(user_id, month)
        return usage["calls_made"] < usage["calls_limit"]

    # ── Analytics ───────────────────────────────────────────────

    async def get_campaign_stats(self, campaign_id: int, user_id: int) -> dict:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT status, COUNT(*) as count
                   FROM candidates
                   WHERE campaign_id = $1 AND user_id = $2
                   GROUP BY status""",
                campaign_id, user_id,
            )
            by_status = {r["status"]: r["count"] for r in rows}
            total = sum(by_status.values())
            return {
                "total": total,
                "by_status": by_status,
                "called": total - by_status.get("PENDING", 0),
                "pending": by_status.get("PENDING", 0),
            }

    async def get_user_stats(self, user_id: int) -> dict:
        """Overall stats across all campaigns."""
        async with self._pool.acquire() as conn:
            campaign_count = await conn.fetchval(
                "SELECT COUNT(*) FROM campaigns WHERE user_id = $1", user_id
            )
            total_candidates = await conn.fetchval(
                "SELECT COUNT(*) FROM candidates WHERE user_id = $1", user_id
            )
            status_rows = await conn.fetch(
                """SELECT status, COUNT(*) as count
                   FROM candidates WHERE user_id = $1
                   GROUP BY status""",
                user_id,
            )
            by_status = {r["status"]: r["count"] for r in status_rows}

            month = datetime.now(timezone.utc).strftime("%Y-%m")
            usage = await self.get_usage(user_id, month)

            return {
                "campaigns": campaign_count,
                "total_candidates": total_candidates,
                "by_status": by_status,
                "calls_this_month": usage["calls_made"],
                "monthly_limit": usage["calls_limit"],
            }

    # ── Call log operations ─────────────────────────────────────

    async def log_call_event(
        self,
        user_id: int,
        campaign_id: int,
        candidate_id: int,
        vapi_call_id: str = "",
        action: str = "",
        status: str = "",
        detail: str = "",
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO call_logs
                   (user_id, campaign_id, candidate_id, vapi_call_id, action, status, detail)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                user_id, campaign_id, candidate_id,
                vapi_call_id, action, status, detail,
            )
