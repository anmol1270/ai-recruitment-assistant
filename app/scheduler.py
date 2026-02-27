"""
Call scheduler — enforces UK calling windows, throttling limits,
and retry policies. Drives the outbound calling loop.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

import structlog

from app.config import Settings
from app.database import Database
from app.models import CallRecord, Disposition
from app.vapi_client import VAPIClient

log = structlog.get_logger(__name__)


class CallScheduler:
    """
    Orchestrates outbound calls with:
      - UK calling-window enforcement
      - Hourly / daily throttle limits
      - Configurable retry policy
      - Concurrency control (semaphore)
    """

    def __init__(
        self,
        settings: Settings,
        db: Database,
        vapi: VAPIClient,
        run_id: str | None = None,
    ):
        self.settings = settings
        self.db = db
        self.vapi = vapi
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self._tz = ZoneInfo(settings.calling_timezone)
        self._sem = asyncio.Semaphore(settings.max_concurrent_calls)
        self._calls_this_hour = 0
        self._calls_today = 0
        self._hour_start = datetime.now(self._tz)
        self._day_start = datetime.now(self._tz).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    # ── Public API ──────────────────────────────────────────────

    async def run_batch(self, assistant_id: str) -> dict:
        """
        Main entry point: fetch pending records, place calls respecting
        all constraints, and return a summary.

        Returns a dict with counts of calls placed, skipped, etc.
        """
        stats = {"placed": 0, "skipped_window": 0, "skipped_throttle": 0, "errors": 0}

        # Refresh throttle counters from DB
        await self._refresh_counters()

        records = await self.db.get_pending_records(limit=self.settings.max_calls_per_day)

        if not records:
            log.info("no_pending_records")
            return stats

        log.info(
            "batch_starting",
            run_id=self.run_id,
            pending=len(records),
            calls_today=self._calls_today,
            max_daily=self.settings.max_calls_per_day,
        )

        tasks = []
        for record in records:
            # Check daily limit
            if self._calls_today >= self.settings.max_calls_per_day:
                stats["skipped_throttle"] += 1
                continue

            # Check hourly limit
            if self._calls_this_hour >= self.settings.max_calls_per_hour:
                stats["skipped_throttle"] += 1
                continue

            # Check calling window
            if not self._in_calling_window():
                stats["skipped_window"] += 1
                continue

            # Check retry delay
            if not self._retry_eligible(record):
                stats["skipped_throttle"] += 1
                continue

            tasks.append(self._place_single_call(record, assistant_id, stats))

        if tasks:
            await asyncio.gather(*tasks)

        log.info("batch_complete", run_id=self.run_id, **stats)
        return stats

    async def wait_for_calling_window(self) -> None:
        """Sleep until the next calling window opens."""
        while not self._in_calling_window():
            now = datetime.now(self._tz)
            window_start = self._parse_time(self.settings.calling_window_start)

            if now.time() >= self._parse_time(self.settings.calling_window_end):
                # Window closed for today — sleep until tomorrow's start
                tomorrow = now.date() + timedelta(days=1)
                next_open = datetime.combine(tomorrow, window_start, tzinfo=self._tz)
            else:
                # Before window — sleep until today's start
                next_open = datetime.combine(now.date(), window_start, tzinfo=self._tz)

            wait_seconds = (next_open - now).total_seconds()
            log.info(
                "waiting_for_calling_window",
                next_open=next_open.isoformat(),
                wait_minutes=round(wait_seconds / 60, 1),
            )
            await asyncio.sleep(min(wait_seconds + 5, 300))  # Re-check every 5 min max

    # ── Internal helpers ────────────────────────────────────────

    async def _place_single_call(
        self,
        record: CallRecord,
        assistant_id: str,
        stats: dict,
    ) -> None:
        """Place one call with concurrency control."""
        async with self._sem:
            try:
                # Double-check window + throttle under semaphore
                if not self._in_calling_window():
                    stats["skipped_window"] += 1
                    return
                if self._calls_this_hour >= self.settings.max_calls_per_hour:
                    stats["skipped_throttle"] += 1
                    return

                # Idempotency: skip if this record already has an active call
                fresh = await self.db.get_record_by_id(record.unique_record_id)
                if fresh and fresh.status not in (
                    Disposition.PENDING,
                    Disposition.NO_ANSWER,
                    Disposition.BUSY,
                    Disposition.FAILED,
                ):
                    log.info(
                        "skipping_already_resolved",
                        record_id=record.unique_record_id,
                        status=fresh.status.value,
                    )
                    return

                # Place the call via VAPI
                result = await self.vapi.place_call(
                    phone_e164=record.phone_e164,
                    assistant_id=assistant_id,
                    candidate_name=record.unique_record_id,  # Will be overridden with first_name if available
                    record_id=record.unique_record_id,
                )

                vapi_call_id = result.get("id", "")

                # Update DB
                await self.db.mark_call_started(record.unique_record_id, vapi_call_id)

                # Log the event
                await self.db.log_run_event(
                    run_id=self.run_id,
                    unique_record_id=record.unique_record_id,
                    action="call_placed",
                    vapi_call_id=vapi_call_id,
                    status="in_progress",
                )

                self._calls_this_hour += 1
                self._calls_today += 1
                stats["placed"] += 1

                # Small delay between calls to avoid burst
                await asyncio.sleep(2)

            except Exception as e:
                log.error(
                    "call_placement_error",
                    record_id=record.unique_record_id,
                    error=str(e),
                )
                await self.db.log_run_event(
                    run_id=self.run_id,
                    unique_record_id=record.unique_record_id,
                    action="call_error",
                    status="error",
                    detail=str(e),
                )
                stats["errors"] += 1

    def _in_calling_window(self) -> bool:
        """Check if current time is within the UK calling window."""
        now = datetime.now(self._tz).time()
        start = self._parse_time(self.settings.calling_window_start)
        end = self._parse_time(self.settings.calling_window_end)
        return start <= now <= end

    def _retry_eligible(self, record: CallRecord) -> bool:
        """Check if enough time has passed since last attempt for a retry."""
        if record.attempt_count == 0:
            return True
        if record.attempt_count > self.settings.max_retries:
            return False
        if record.last_called_at is None:
            return True

        min_wait = timedelta(minutes=self.settings.retry_delay_minutes)
        elapsed = datetime.utcnow() - record.last_called_at
        return elapsed >= min_wait

    async def _refresh_counters(self) -> None:
        """Refresh hourly/daily call counters from the database."""
        now = datetime.now(self._tz)

        # Reset hour counter if needed
        if (now - self._hour_start).total_seconds() >= 3600:
            self._hour_start = now
            self._calls_this_hour = 0
        else:
            hour_ago = (now - timedelta(hours=1)).isoformat()
            self._calls_this_hour = await self.db.get_calls_in_window(hour_ago)

        # Reset day counter if needed
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if now.date() != self._day_start.date():
            self._day_start = today_start
            self._calls_today = 0
        else:
            self._calls_today = await self.db.get_calls_in_window(
                today_start.isoformat()
            )

    @staticmethod
    def _parse_time(time_str: str) -> dt_time:
        """Parse 'HH:MM' string to time object."""
        parts = time_str.strip().split(":")
        return dt_time(int(parts[0]), int(parts[1]))
