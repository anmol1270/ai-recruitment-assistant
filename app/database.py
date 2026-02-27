"""
SQLite-backed persistence layer using aiosqlite.
Handles call records, run logs, idempotency checks.
"""

from __future__ import annotations

import aiosqlite
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.models import CallRecord, Disposition

_SCHEMA = """
CREATE TABLE IF NOT EXISTS call_records (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    unique_record_id     TEXT NOT NULL,
    phone_e164           TEXT NOT NULL,
    vapi_call_id         TEXT DEFAULT '',
    status               TEXT DEFAULT 'PENDING',
    short_summary        TEXT DEFAULT '',
    attempt_count        INTEGER DEFAULT 0,
    last_called_at       TEXT,
    raw_call_outcome     TEXT DEFAULT '',
    transcript           TEXT DEFAULT '',
    recording_url        TEXT DEFAULT '',
    extracted_location   TEXT DEFAULT '',
    extracted_availability TEXT DEFAULT '',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    UNIQUE(unique_record_id)
);

CREATE TABLE IF NOT EXISTS run_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT NOT NULL,
    unique_record_id TEXT NOT NULL,
    vapi_call_id     TEXT DEFAULT '',
    action           TEXT NOT NULL,
    status           TEXT DEFAULT '',
    detail           TEXT DEFAULT '',
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_call_records_status ON call_records(status);
CREATE INDEX IF NOT EXISTS idx_call_records_phone ON call_records(phone_e164);
CREATE INDEX IF NOT EXISTS idx_run_log_run ON run_log(run_id);
"""


class Database:
    """Async SQLite wrapper for the calling platform."""

    def __init__(self, db_path: Path):
        self._path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ── Call records ────────────────────────────────────────────

    async def upsert_candidate(self, record: CallRecord) -> None:
        """Insert or ignore a candidate record (idempotent on unique_record_id)."""
        await self._db.execute(
            """
            INSERT INTO call_records
                (unique_record_id, phone_e164, status, attempt_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(unique_record_id) DO UPDATE SET
                phone_e164 = excluded.phone_e164,
                updated_at = excluded.updated_at
            """,
            (
                record.unique_record_id,
                record.phone_e164,
                record.status.value,
                record.attempt_count,
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_pending_records(self, limit: int = 50) -> list[CallRecord]:
        """Fetch records that are PENDING or eligible for retry."""
        cursor = await self._db.execute(
            """
            SELECT * FROM call_records
            WHERE status IN ('PENDING', 'NO_ANSWER', 'BUSY', 'FAILED')
              AND attempt_count < ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (3, limit),  # max_retries + 1
        )
        rows = await cursor.fetchall()
        return [self._row_to_record(r) for r in rows]

    async def get_record_by_id(self, unique_record_id: str) -> Optional[CallRecord]:
        cursor = await self._db.execute(
            "SELECT * FROM call_records WHERE unique_record_id = ?",
            (unique_record_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_record(row) if row else None

    async def get_record_by_call_id(self, vapi_call_id: str) -> Optional[CallRecord]:
        cursor = await self._db.execute(
            "SELECT * FROM call_records WHERE vapi_call_id = ?",
            (vapi_call_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_record(row) if row else None

    async def mark_call_started(
        self, unique_record_id: str, vapi_call_id: str
    ) -> None:
        now = datetime.utcnow().isoformat()
        await self._db.execute(
            """
            UPDATE call_records
            SET vapi_call_id = ?,
                attempt_count = attempt_count + 1,
                last_called_at = ?,
                updated_at = ?
            WHERE unique_record_id = ?
            """,
            (vapi_call_id, now, now, unique_record_id),
        )
        await self._db.commit()

    async def update_call_result(
        self,
        vapi_call_id: str,
        status: Disposition,
        short_summary: str = "",
        raw_call_outcome: str = "",
        transcript: str = "",
        recording_url: str = "",
        extracted_location: str = "",
        extracted_availability: str = "",
    ) -> None:
        now = datetime.utcnow().isoformat()
        await self._db.execute(
            """
            UPDATE call_records
            SET status = ?,
                short_summary = ?,
                raw_call_outcome = ?,
                transcript = ?,
                recording_url = ?,
                extracted_location = ?,
                extracted_availability = ?,
                updated_at = ?
            WHERE vapi_call_id = ?
            """,
            (
                status.value,
                short_summary,
                raw_call_outcome,
                transcript,
                recording_url,
                extracted_location,
                extracted_availability,
                now,
                vapi_call_id,
            ),
        )
        await self._db.commit()

    async def get_all_records(self) -> list[CallRecord]:
        cursor = await self._db.execute(
            "SELECT * FROM call_records ORDER BY created_at ASC"
        )
        rows = await cursor.fetchall()
        return [self._row_to_record(r) for r in rows]

    async def get_calls_in_window(self, since_iso: str) -> int:
        """Count calls placed since a given ISO timestamp."""
        cursor = await self._db.execute(
            "SELECT COUNT(*) as cnt FROM call_records WHERE last_called_at >= ?",
            (since_iso,),
        )
        row = await cursor.fetchone()
        return row["cnt"] if row else 0

    # ── Run log ─────────────────────────────────────────────────

    async def log_run_event(
        self,
        run_id: str,
        unique_record_id: str,
        action: str,
        vapi_call_id: str = "",
        status: str = "",
        detail: str = "",
    ) -> None:
        now = datetime.utcnow().isoformat()
        await self._db.execute(
            """
            INSERT INTO run_log
                (run_id, unique_record_id, vapi_call_id, action, status, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, unique_record_id, vapi_call_id, action, status, detail, now),
        )
        await self._db.commit()

    # ── Helpers ─────────────────────────────────────────────────

    @staticmethod
    def _row_to_record(row) -> CallRecord:
        return CallRecord(
            id=row["id"],
            unique_record_id=row["unique_record_id"],
            phone_e164=row["phone_e164"],
            vapi_call_id=row["vapi_call_id"],
            status=Disposition(row["status"]),
            short_summary=row["short_summary"],
            attempt_count=row["attempt_count"],
            last_called_at=datetime.fromisoformat(row["last_called_at"])
            if row["last_called_at"]
            else None,
            raw_call_outcome=row["raw_call_outcome"],
            transcript=row["transcript"],
            recording_url=row["recording_url"],
            extracted_location=row["extracted_location"],
            extracted_availability=row["extracted_availability"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
