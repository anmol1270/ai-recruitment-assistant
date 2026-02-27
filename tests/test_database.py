"""Tests for database operations."""

import pytest
import pytest_asyncio
from datetime import datetime
from pathlib import Path

from app.database import Database
from app.models import CallRecord, Disposition


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_upsert_candidate(db):
    record = CallRecord(
        unique_record_id="R001",
        phone_e164="+447700900001",
        status=Disposition.PENDING,
    )
    await db.upsert_candidate(record)

    fetched = await db.get_record_by_id("R001")
    assert fetched is not None
    assert fetched.phone_e164 == "+447700900001"
    assert fetched.status == Disposition.PENDING


@pytest.mark.asyncio
async def test_upsert_idempotent(db):
    record = CallRecord(
        unique_record_id="R001",
        phone_e164="+447700900001",
        status=Disposition.PENDING,
    )
    await db.upsert_candidate(record)
    await db.upsert_candidate(record)  # should not duplicate

    all_records = await db.get_all_records()
    assert len(all_records) == 1


@pytest.mark.asyncio
async def test_get_pending_records(db):
    for i in range(5):
        record = CallRecord(
            unique_record_id=f"R{i:03}",
            phone_e164=f"+44770090000{i}",
            status=Disposition.PENDING,
        )
        await db.upsert_candidate(record)

    pending = await db.get_pending_records(limit=3)
    assert len(pending) == 3


@pytest.mark.asyncio
async def test_mark_call_started(db):
    record = CallRecord(
        unique_record_id="R001",
        phone_e164="+447700900001",
        status=Disposition.PENDING,
    )
    await db.upsert_candidate(record)
    await db.mark_call_started("R001", "vapi_call_123")

    fetched = await db.get_record_by_id("R001")
    assert fetched.vapi_call_id == "vapi_call_123"
    assert fetched.attempt_count == 1
    assert fetched.last_called_at is not None


@pytest.mark.asyncio
async def test_update_call_result(db):
    record = CallRecord(
        unique_record_id="R001",
        phone_e164="+447700900001",
        status=Disposition.PENDING,
    )
    await db.upsert_candidate(record)
    await db.mark_call_started("R001", "vapi_call_123")

    await db.update_call_result(
        vapi_call_id="vapi_call_123",
        status=Disposition.ACTIVE_LOOKING,
        short_summary="Candidate is actively looking for roles in London.",
        raw_call_outcome="completed",
        transcript="...",
        extracted_location="London",
        extracted_availability="Immediate",
    )

    fetched = await db.get_record_by_call_id("vapi_call_123")
    assert fetched.status == Disposition.ACTIVE_LOOKING
    assert "London" in fetched.short_summary
    assert fetched.extracted_location == "London"


@pytest.mark.asyncio
async def test_run_log(db):
    await db.log_run_event(
        run_id="run_001",
        unique_record_id="R001",
        action="call_placed",
        vapi_call_id="vapi_123",
        status="in_progress",
    )
    # No assertion needed — just ensure no errors
